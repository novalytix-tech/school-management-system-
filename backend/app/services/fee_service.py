"""
Fee ledger domain logic: invoice arithmetic, installments, payments, receipts,
refund bookkeeping, caller visibility and serialization.

The server is the single source of truth for every amount. Invariants kept
here (see docs/PAYMENTS_API.md):

* ``total = subtotal - discount_amount + fine_amount``
* ``balance = total - paid_amount + refunded_amount``
* ``sum(installment.amount) == total`` and
  ``sum(installment.paid_amount) == paid_amount - refunded_amount``
* a receipt exists only for a recorded payment, and its snapshot is frozen.

Callers own the transaction (commit/rollback); helpers only flush.
"""
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask_jwt_extended import get_jwt, get_jwt_identity
from sqlalchemy import text

from app.extensions import db
from app.models import (
    FeeInvoice, FeePayment, FinancialTransaction, FeeInvoiceAdjustment, FeeInstallment,
    FeeReceipt, FeeRefund, PaymentOrder, Institution, Student, StudentEnrollment, StudentParent, Parent,
    AcademicYear,
)
from app.utils.errors import APIException
from app.services.security_audit import record_security_event

logger = logging.getLogger("fees")

# Numeric(12, 2) upper bound.
MAX_MONEY = Decimal("9999999999.99")
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
IST = timezone(timedelta(hours=5, minutes=30), "IST")

# Invoices in these states can no longer accept payments.
NON_PAYABLE_STATUSES = {"PAID", "CANCELLED", "CANCELED", "VOID", "WAIVED", "REFUNDED"}
# Lifecycle states that are never recomputed from amounts.
FROZEN_STATUSES = {"CANCELLED", "CANCELED", "VOID", "WAIVED"}

STAFF_ROLES = {"super_admin", "school_admin", "teacher"}
ADMIN_ROLES = {"super_admin", "school_admin"}


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def parse_uuid(value, field: str) -> uuid.UUID:
    """Parse a UUID from request input, raising a 400 instead of a 500."""
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise APIException(f"{field} must be a valid UUID", status_code=400, error_code="INVALID_ID")
    try:
        return uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        raise APIException(f"{field} must be a valid UUID", status_code=400, error_code="INVALID_ID")


def parse_money(value, field: str = "amount", allow_zero: bool = False) -> Decimal:
    """Parse a monetary amount strictly: finite, 2 decimal places, positive (or >= 0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise APIException(f"{field} must be a number", status_code=400, error_code="INVALID_AMOUNT")
    try:
        amount = Decimal(str(value).strip())
        if not amount.is_finite():
            raise InvalidOperation
        amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise APIException(f"{field} must be a finite number", status_code=400, error_code="INVALID_AMOUNT")
    if amount < 0 or (amount == 0 and not allow_zero):
        qualifier = "zero or more" if allow_zero else "greater than zero"
        raise APIException(f"{field} must be {qualifier}", status_code=400, error_code="INVALID_AMOUNT")
    if amount > MAX_MONEY:
        raise APIException(f"{field} is too large", status_code=400, error_code="INVALID_AMOUNT")
    return amount


def parse_date(value, field: str) -> date:
    if not isinstance(value, str):
        raise APIException(f"{field} must be a date in YYYY-MM-DD format", status_code=400, error_code="INVALID_DATE")
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise APIException(f"{field} must be a date in YYYY-MM-DD format", status_code=400, error_code="INVALID_DATE")


def parse_label(value, field: str, max_len: int, required: bool = True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_len:
        raise APIException(f"{field} must be a non-empty string (max {max_len} characters)",
                           status_code=400, error_code="INVALID_INPUT")
    return value.strip()


def q2(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def to_paise(amount: Decimal) -> int:
    return int((q2(amount) * 100).to_integral_value())


def money(value) -> float:
    return float(q2(value))


def iso(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return dt.isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_local() -> date:
    """School-local (IST) calendar date used for due/overdue decisions."""
    return datetime.now(IST).date()


def new_payment_number() -> str:
    """Collision-free payment number (a timestamp alone collides within one second)."""
    return f"PAY-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


def new_invoice_number() -> str:
    return f"INV-{today_local():%Y%m}-{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# Caller identity & visibility (IDOR protection)
# ---------------------------------------------------------------------------

def current_user_uuid():
    try:
        identity = get_jwt_identity()
        return uuid.UUID(str(identity)) if identity else None
    except Exception:
        return None


def current_roles() -> set:
    try:
        return set((get_jwt() or {}).get("roles", []) or [])
    except Exception:
        return set()


def is_staff(roles=None) -> bool:
    return bool((roles if roles is not None else current_roles()) & STAFF_ROLES)


def is_admin(roles=None) -> bool:
    return bool((roles if roles is not None else current_roles()) & ADMIN_ROLES)


def own_student_ids(institution_id) -> set:
    """Students the caller owns as the student or as a parent/guardian."""
    roles = current_roles()
    user_id = current_user_uuid()
    ids = set()
    if not user_id:
        return ids
    if "student" in roles:
        ids.update(sid for (sid,) in db.session.query(Student.id).filter(
            Student.user_id == user_id, Student.institution_id == institution_id,
            Student.is_deleted.is_(False)))
    if "parent" in roles:
        ids.update(sid for (sid,) in db.session.query(StudentParent.student_id)
                   .join(Parent, StudentParent.parent_id == Parent.id)
                   .join(Student, StudentParent.student_id == Student.id)
                   .filter(Parent.user_id == user_id, Student.institution_id == institution_id,
                           Student.is_deleted.is_(False)))
    return ids


def accessible_student(student_id, institution_id, *, staff_roles=STAFF_ROLES) -> Student:
    """Load a student the caller may see; unknown and foreign ids look identical (404)."""
    student = Student.query.filter(
        Student.id == student_id, Student.institution_id == institution_id, Student.is_deleted.is_(False)
    ).first()
    if student is None:
        raise APIException("Student not found", status_code=404, error_code="STUDENT_NOT_FOUND")
    if current_roles() & staff_roles:
        return student
    if student.id in own_student_ids(institution_id):
        return student
    raise APIException("Student not found", status_code=404, error_code="STUDENT_NOT_FOUND")


def visible_students(institution_id, student_id_raw=None, *, require_for_staff=True):
    """Students whose fees the caller may see, optionally narrowed to one id."""
    if student_id_raw:
        return [accessible_student(parse_uuid(student_id_raw, "student_id"), institution_id)]
    if is_staff():
        if require_for_staff:
            raise APIException("student_id is required", status_code=400, error_code="STUDENT_REQUIRED")
        return None  # all students
    ids = own_student_ids(institution_id)
    if not ids:
        return []
    return (Student.query.filter(Student.id.in_(ids))
            .order_by(Student.admission_number.asc()).all())


# ---------------------------------------------------------------------------
# Invoice arithmetic & installments
# ---------------------------------------------------------------------------

def net_paid(inv: FeeInvoice) -> Decimal:
    return q2(inv.paid_amount) - q2(inv.refunded_amount)


def installment_status(inst: FeeInstallment, today=None) -> str:
    today = today or today_local()
    amount, paid = q2(inst.amount), q2(inst.paid_amount)
    if paid >= amount:
        return "PAID"
    if inst.due_date < today:
        return "OVERDUE"
    if paid > 0:
        return "PARTIALLY_PAID"
    return "PENDING"


def refresh_installment_statuses(inv: FeeInvoice):
    today = today_local()
    for inst in inv.installments:
        inst.status = installment_status(inst, today)


def compute_invoice_status(inv: FeeInvoice) -> str:
    if (inv.status or "").upper() in FROZEN_STATUSES:
        return inv.status
    if q2(inv.balance_amount) <= 0:
        return "PAID"
    if net_paid(inv) > 0:
        return "PARTIALLY_PAID"
    return "UNPAID"


def allocate_installments(inv: FeeInvoice, amount: Decimal):
    """Apply a payment to installments in due-date order."""
    remaining = q2(amount)
    for inst in sorted(inv.installments, key=lambda i: (i.due_date, i.installment_number)):
        if remaining <= 0:
            break
        room = q2(inst.amount) - q2(inst.paid_amount)
        if room <= 0:
            continue
        take = min(room, remaining)
        inst.paid_amount = q2(inst.paid_amount) + take
        remaining -= take
    refresh_installment_statuses(inv)


def deallocate_installments(inv: FeeInvoice, amount: Decimal):
    """Reverse a refund from the latest-due installments first."""
    remaining = q2(amount)
    for inst in sorted(inv.installments, key=lambda i: (i.due_date, i.installment_number), reverse=True):
        if remaining <= 0:
            break
        take = min(q2(inst.paid_amount), remaining)
        inst.paid_amount = q2(inst.paid_amount) - take
        remaining -= take
    refresh_installment_statuses(inv)


def rebalance_installments(inv: FeeInvoice):
    """Keep sum(installments) == total: the difference lands on the last installment(s)."""
    insts = sorted(inv.installments, key=lambda i: (i.due_date, i.installment_number))
    if not insts:
        return
    delta = q2(inv.total_amount) - sum((q2(i.amount) for i in insts), ZERO)
    if delta > 0:
        insts[-1].amount = q2(insts[-1].amount) + delta
    elif delta < 0:
        need = -delta
        for inst in reversed(insts):
            if need <= 0:
                break
            reducible = q2(inst.amount) - q2(inst.paid_amount)
            take = min(reducible, need)
            inst.amount = q2(inst.amount) - take
            need -= take
        if need > 0:  # pragma: no cover - guarded by recompute_invoice_totals
            raise APIException("Installments cannot absorb this change", status_code=400,
                               error_code="INVALID_ADJUSTMENT")
    refresh_installment_statuses(inv)


def recompute_invoice_totals(inv: FeeInvoice):
    """Derive discount/fine/total/balance from the adjustments and re-balance installments."""
    discount = sum((q2(a.amount) for a in inv.adjustments if a.kind in FeeInvoiceAdjustment.REDUCING_KINDS), ZERO)
    fine = sum((q2(a.amount) for a in inv.adjustments if a.kind == "LATE_FINE"), ZERO)
    total = q2(inv.subtotal) - discount + fine
    if total < 0:
        raise APIException("Discounts cannot exceed the invoice amount", status_code=400,
                           error_code="INVALID_ADJUSTMENT")
    if total < net_paid(inv):
        raise APIException(
            f"The invoice total ({total}) cannot go below the amount already paid ({net_paid(inv)}); "
            "refund the payment first", status_code=400, error_code="INVALID_ADJUSTMENT")
    inv.discount_amount = discount
    inv.fine_amount = fine
    inv.total_amount = total
    inv.balance_amount = total - q2(inv.paid_amount) + q2(inv.refunded_amount)
    inv.status = compute_invoice_status(inv)
    rebalance_installments(inv)


def build_installments(inv: FeeInvoice, *, installments=None, installment_count=None):
    """Create the installment plan for a new invoice (default: one installment)."""
    total = q2(inv.total_amount)
    plan = []
    if installments:
        if not isinstance(installments, list) or len(installments) > 24:
            raise APIException("installments must be a list (max 24)", status_code=400, error_code="INVALID_INPUT")
        for idx, raw in enumerate(installments):
            if not isinstance(raw, dict):
                raise APIException(f"installments[{idx}] must be an object", status_code=400,
                                   error_code="INVALID_INPUT")
            label = parse_label(raw.get("label") or f"Installment {idx + 1}", f"installments[{idx}].label", 100)
            amount = parse_money(raw.get("amount"), f"installments[{idx}].amount", allow_zero=True)
            due = parse_date(raw.get("due_date"), f"installments[{idx}].due_date")
            plan.append((label, amount, due))
        if sum((a for _, a, _ in plan), ZERO) != total:
            raise APIException(f"Installment amounts must add up to the invoice total ({total})",
                               status_code=400, error_code="INSTALLMENT_MISMATCH")
    else:
        count = 1 if installment_count is None else installment_count
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 12:
            raise APIException("installment_count must be an integer between 1 and 12",
                               status_code=400, error_code="INVALID_INPUT")
        share = (total / count).quantize(CENT, rounding="ROUND_DOWN")
        for i in range(count):
            amount = share if i < count - 1 else total - share * (count - 1)
            label = "Full payment" if count == 1 else f"Installment {i + 1} of {count}"
            plan.append((label, amount, _add_months(inv.due_date, i)))
    plan.sort(key=lambda p: p[2])
    for number, (label, amount, due) in enumerate(plan, start=1):
        inv.installments.append(FeeInstallment(
            installment_number=number, label=label, amount=amount, due_date=due,
            paid_amount=ZERO, status="PENDING"))
    inv.due_date = plan[-1][2]
    refresh_installment_statuses(inv)


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year, month = d.year + month_index // 12, month_index % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 28)  # pragma: no cover


def installment_remaining(inst) -> Decimal:
    return max(q2(inst.amount) - q2(inst.paid_amount), ZERO)


def overdue_amount(inv: FeeInvoice) -> Decimal:
    if q2(inv.balance_amount) <= 0 or (inv.status or "").upper() in FROZEN_STATUSES:
        return ZERO
    today = today_local()
    if inv.installments:
        due = sum((installment_remaining(i) for i in inv.installments if i.due_date < today), ZERO)
        return min(due, q2(inv.balance_amount))
    return q2(inv.balance_amount) if inv.due_date < today else ZERO


def is_invoice_overdue(inv: FeeInvoice) -> bool:
    return overdue_amount(inv) > 0


def next_payable_amount(inv: FeeInvoice) -> Decimal:
    """Overdue installments first, else the next unpaid installment; never above the balance."""
    balance = q2(inv.balance_amount)
    if balance <= 0 or (inv.status or "").upper() in NON_PAYABLE_STATUSES - {"PAID"}:
        return ZERO
    insts = sorted(inv.installments, key=lambda i: (i.due_date, i.installment_number))
    if not insts:
        return balance
    overdue = overdue_amount(inv)
    if overdue > 0:
        return min(overdue, balance)
    for inst in insts:
        rem = installment_remaining(inst)
        if rem > 0:
            return min(rem, balance)
    return balance


def is_payable(inv: FeeInvoice) -> bool:
    return (inv.status or "").upper() not in NON_PAYABLE_STATUSES and q2(inv.balance_amount) > 0


# ---------------------------------------------------------------------------
# Payments, ledger & receipts
# ---------------------------------------------------------------------------

def lock_invoice(invoice_id, institution_id) -> FeeInvoice:
    inv = (FeeInvoice.query.filter_by(id=invoice_id, institution_id=institution_id)
           .populate_existing().with_for_update().first())
    if not inv or inv.is_deleted:
        raise APIException("Invoice not found", status_code=404)
    return inv


def record_payment(invoice_id, institution_id, amount: Decimal, method: str,
                   reference=None, recorded_by=None, payment_number=None, *,
                   is_test=False, order=None, issue_receipt=True, instrument=None):
    """
    Apply a payment to an invoice under a row lock, allocate it to installments,
    write the ledger entry and (by default) issue the receipt.

    The invoice row is locked (SELECT ... FOR UPDATE) for the whole
    read-modify-write so concurrent payments serialize instead of overwriting
    each other. Raises APIException (nothing written) when the invoice cannot
    absorb the amount. Returns (invoice, payment); the caller commits.
    """
    inv = lock_invoice(invoice_id, institution_id)
    if not is_payable(inv):
        raise APIException(f"Invoice is {inv.status} and cannot accept payments",
                           status_code=409, error_code="INVOICE_NOT_PAYABLE")
    if amount > q2(inv.balance_amount):
        raise APIException(
            f"Payment amount ({amount}) cannot exceed remaining balance ({inv.balance_amount})",
            status_code=400, error_code="OVERPAYMENT",
        )

    payment = FeePayment(
        invoice_id=inv.id,
        payment_number=payment_number or new_payment_number(),
        amount=amount,
        payment_method=method,
        transaction_reference=reference,
        recorded_by=recorded_by,
        status="SUCCESS",
        refunded_amount=ZERO,
        is_test=bool(is_test),
        order_id=order.id if order is not None else None,
    )
    db.session.add(payment)
    # Assign payment.id now so the ledger row can reference it.
    db.session.flush()

    inv.paid_amount = q2(inv.paid_amount) + amount
    inv.balance_amount = q2(inv.balance_amount) - amount
    inv.status = compute_invoice_status(inv)
    allocate_installments(inv, amount)

    db.session.add(FinancialTransaction(
        institution_id=inv.institution_id,
        payment_id=payment.id,
        transaction_type="FEE_PAYMENT",
        category="REVENUE",
        amount=amount,
        credit_or_debit="CREDIT",
        description=f"Fee collection for {inv.invoice_number} via {method}" + (" [TEST]" if is_test else ""),
    ))
    # Financial audit trail: amount, invoice and method only (no card/bank data).
    record_security_event(
        "PAYMENT_RECORDED", inv.id, entity_type="FeeInvoice",
        actor_user_id=recorded_by, institution_id=inv.institution_id,
        summary=f"{amount} recorded against {inv.invoice_number} via {method}; status {inv.status}."
                + (" TEST payment." if is_test else ""),
    )
    db.session.flush()
    if issue_receipt:
        issue_receipt_for(payment, inv, order=order, instrument=instrument)
    return inv, payment


def _post_ledger_refund(inv: FeeInvoice, payment: FeePayment, amount: Decimal, note: str):
    db.session.add(FinancialTransaction(
        institution_id=inv.institution_id,
        payment_id=payment.id,
        transaction_type="REFUND",
        category="REVENUE",
        amount=amount,
        credit_or_debit="DEBIT",
        description=note,
    ))


def next_receipt_number(institution_id, issued_at: datetime) -> str:
    """RCT-<YYYY>-<6 digits>, sequential per institution and year.

    A transaction-scoped advisory lock serializes concurrent issuers for the
    same institution/year, so two receipts can never draw the same number
    (the UNIQUE constraint is the backstop).
    """
    year = issued_at.astimezone(IST).year
    prefix = f"RCT-{year}-"
    db.session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                       {"k": f"fee_receipt:{institution_id}:{year}"})
    last = db.session.execute(text(
        "SELECT receipt_number FROM fee_receipts WHERE institution_id = :i AND receipt_number LIKE :p "
        "ORDER BY receipt_number DESC LIMIT 1"), {"i": institution_id, "p": prefix + "%"}).scalar()
    seq = 1
    if last:
        try:
            seq = int(last[len(prefix):]) + 1
        except ValueError:  # pragma: no cover
            seq = 1
    return f"{prefix}{seq:06d}"


def student_profile(student: Student) -> dict:
    """Name, admission number, class/section, guardian and session of a student."""
    name = "Student"
    if student.user:
        name = f"{student.user.first_name} {student.user.last_name}".strip() or name
    enrollment = (StudentEnrollment.query
                  .filter(StudentEnrollment.student_id == student.id, StudentEnrollment.status == "ACTIVE",
                          StudentEnrollment.is_deleted.is_(False))
                  .join(AcademicYear, StudentEnrollment.academic_year_id == AcademicYear.id)
                  .order_by(AcademicYear.start_date.desc())
                  .first())
    class_name = section_name = session = None
    if enrollment:
        if enrollment.section:
            section_name = enrollment.section.name
            if enrollment.section.class_level:
                class_name = enrollment.section.class_level.name
        if enrollment.academic_year:
            session = enrollment.academic_year.name
    link = (StudentParent.query.join(Parent, StudentParent.parent_id == Parent.id)
            .filter(StudentParent.student_id == student.id, Parent.is_deleted.is_(False))
            .order_by(StudentParent.is_primary_contact.desc(), StudentParent.created_at.asc())
            .first())
    guardian = None
    if link and link.parent:
        guardian = f"{link.parent.first_name} {link.parent.last_name}".strip()
    elif student.emergency_contact_name:
        guardian = student.emergency_contact_name
    return {
        "id": str(student.id),
        "name": name,
        "admission_number": student.admission_number,
        "class_name": class_name,
        "section_name": section_name,
        "guardian_name": guardian,
        "academic_session": session,
    }


def build_receipt_snapshot(payment: FeePayment, inv: FeeInvoice, receipt_number: str, issued_at: datetime,
                           order: PaymentOrder = None, instrument=None) -> dict:
    institution = db.session.get(Institution, inv.institution_id)
    student = inv.student or db.session.get(Student, inv.student_id)
    profile = student_profile(student)
    if not profile["academic_session"] and inv.fee_structure and inv.fee_structure.academic_year:
        profile["academic_session"] = inv.fee_structure.academic_year.name
    adjustments = [{"kind": a.kind, "label": a.label, "amount": money(a.amount)} for a in inv.adjustments]
    scholarships = sum((q2(a.amount) for a in inv.adjustments if a.kind == "SCHOLARSHIP"), ZERO)
    provider = order.provider if order is not None else "OFFLINE"
    address = ", ".join(p for p in [
        getattr(institution, "address", None), getattr(institution, "city", None),
        getattr(institution, "state", None), getattr(institution, "postal_code", None)] if p)
    return {
        "version": 1,
        "receipt_number": receipt_number,
        "issued_at": iso(issued_at),
        "is_test": bool(payment.is_test),
        "currency": "INR",
        "school": {
            "name": institution.name if institution else "School",
            "address": address or None,
            "phone": getattr(institution, "phone", None),
            "email": getattr(institution, "email", None),
            "affiliation": " ".join(p for p in [getattr(institution, "board_affiliation", None),
                                                getattr(institution, "affiliation_number", None)] if p) or None,
            "logo_url": getattr(institution, "logo_url", None),
        },
        "student": profile,
        "invoice": {
            "id": str(inv.id),
            "invoice_number": inv.invoice_number,
            "title": inv.fee_structure.name if inv.fee_structure else "Fees",
            "subtotal": money(inv.subtotal),
            "discount_amount": money(inv.discount_amount),
            "scholarship_amount": money(scholarships),
            "fine_amount": money(inv.fine_amount),
            "total_amount": money(inv.total_amount),
            "due_date": iso(inv.due_date),
        },
        "items": [{"fee_head": i.fee_head, "amount": money(i.amount)} for i in inv.items],
        "adjustments": adjustments,
        "installments": [
            {"label": i.label, "amount": money(i.amount), "paid_amount": money(i.paid_amount),
             "due_date": iso(i.due_date), "status": i.status}
            for i in sorted(inv.installments, key=lambda i: (i.due_date, i.installment_number))
        ],
        "payment": {
            "id": str(payment.id),
            "payment_number": payment.payment_number,
            "amount": money(payment.amount),
            "method": payment.payment_method,
            "instrument": instrument,
            "status": "SUCCESS",
            "paid_at": iso(payment.payment_date or issued_at),
            "provider": provider,
            "reference": payment.transaction_reference,
            "order_id": str(order.id) if order is not None else None,
            "gateway_order_id": order.gateway_order_id if order is not None else None,
            "gateway_payment_id": order.gateway_payment_id if order is not None else None,
        },
        "amount_paid": money(payment.amount),
        "discount": money(inv.discount_amount),
        "fine": money(inv.fine_amount),
        "total_paid_to_date": money(net_paid(inv)),
        "balance_after": money(inv.balance_amount),
    }


def issue_receipt_for(payment: FeePayment, inv: FeeInvoice, *, order=None, instrument=None) -> FeeReceipt:
    """Issue the (single) receipt of a recorded payment with a frozen snapshot."""
    existing = FeeReceipt.query.filter_by(payment_id=payment.id).first()
    if existing:
        return existing
    issued_at = now_utc()
    number = next_receipt_number(inv.institution_id, issued_at)
    receipt = FeeReceipt(
        institution_id=inv.institution_id,
        payment_id=payment.id,
        invoice_id=inv.id,
        student_id=inv.student_id,
        receipt_number=number,
        issued_at=issued_at,
        is_test=bool(payment.is_test),
        snapshot=build_receipt_snapshot(payment, inv, number, issued_at, order=order, instrument=instrument),
    )
    db.session.add(receipt)
    db.session.flush()
    return receipt


# ---------------------------------------------------------------------------
# Refund bookkeeping
# ---------------------------------------------------------------------------

def reserved_refund_amount(payment: FeePayment) -> Decimal:
    """Processed + in-flight refunds (pending ones are reserved against over-refunding)."""
    return sum((q2(r.amount) for r in FeeRefund.query.filter(
        FeeRefund.payment_id == payment.id, FeeRefund.status.in_(("PENDING", "PROCESSED"))).all()), ZERO)


def refundable_amount(payment: FeePayment) -> Decimal:
    return max(q2(payment.amount) - reserved_refund_amount(payment), ZERO)


def apply_processed_refund(refund: FeeRefund, actor_user_id=None):
    """Book a processed refund: payment, invoice (balance re-opens), installments, ledger, audit."""
    payment = (FeePayment.query.filter_by(id=refund.payment_id)
               .populate_existing().with_for_update().one())
    inv = (FeeInvoice.query.filter_by(id=payment.invoice_id)
           .populate_existing().with_for_update().one())
    amount = q2(refund.amount)
    if q2(payment.refunded_amount) + amount > q2(payment.amount):
        raise APIException("Refund exceeds the refundable amount", status_code=400, error_code="OVER_REFUND")
    payment.refunded_amount = q2(payment.refunded_amount) + amount
    payment.status = "REFUNDED" if payment.refunded_amount >= q2(payment.amount) else "PARTIALLY_REFUNDED"
    inv.refunded_amount = q2(inv.refunded_amount) + amount
    inv.balance_amount = q2(inv.balance_amount) + amount
    inv.status = compute_invoice_status(inv)
    deallocate_installments(inv, amount)
    refund.status = "PROCESSED"
    refund.processed_at = now_utc()
    _post_ledger_refund(inv, payment, amount,
                        f"Refund of {payment.payment_number} ({inv.invoice_number})"
                        + (" [TEST]" if payment.is_test else ""))
    record_security_event(
        "REFUND_ISSUED", payment.id, entity_type="FeePayment",
        actor_user_id=actor_user_id, institution_id=inv.institution_id,
        summary=f"Refund {amount} of {payment.payment_number} on {inv.invoice_number}; "
                f"payment {payment.status}, invoice balance {inv.balance_amount}.",
    )
    db.session.flush()
    return payment, inv


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_installment(inst: FeeInstallment, today=None) -> dict:
    return {
        "id": str(inst.id),
        "installment_number": inst.installment_number,
        "label": inst.label,
        "amount": money(inst.amount),
        "paid_amount": money(inst.paid_amount),
        "due_date": iso(inst.due_date),
        "status": installment_status(inst, today),
    }


def invoice_balance_fields(inv: FeeInvoice) -> dict:
    return {
        "id": str(inv.id),
        "invoice_number": inv.invoice_number,
        "status": inv.status,
        "subtotal": money(inv.subtotal),
        "discount_amount": money(inv.discount_amount),
        "fine_amount": money(inv.fine_amount),
        "total_amount": money(inv.total_amount),
        "paid_amount": money(inv.paid_amount),
        "refunded_amount": money(inv.refunded_amount),
        "balance_amount": money(inv.balance_amount),
        "next_payable_amount": money(next_payable_amount(inv)),
        "is_overdue": is_invoice_overdue(inv),
    }


def serialize_portal_invoice(inv: FeeInvoice) -> dict:
    today = today_local()
    data = invoice_balance_fields(inv)
    data.update({
        "student_id": str(inv.student_id),
        "fee_structure_id": str(inv.fee_structure_id),
        "title": inv.fee_structure.name if inv.fee_structure else "Fees",
        "issue_date": iso(inv.issue_date),
        "due_date": iso(inv.due_date),
        "items": [{"id": str(i.id), "fee_head": i.fee_head, "amount": money(i.amount)} for i in inv.items],
        "adjustments": [
            {"id": str(a.id), "kind": a.kind, "label": a.label, "amount": money(a.amount),
             "created_at": iso(a.created_at)}
            for a in inv.adjustments
        ],
        "installments": [serialize_installment(i, today) for i in
                         sorted(inv.installments, key=lambda i: (i.due_date, i.installment_number))],
    })
    return data


def serialize_refund(r: FeeRefund) -> dict:
    return {
        "id": str(r.id),
        "payment_id": str(r.payment_id),
        "amount": money(r.amount),
        "status": r.status,
        "reason": r.reason,
        "gateway_refund_id": r.gateway_refund_id,
        "created_at": iso(r.created_at),
        "processed_at": iso(r.processed_at),
    }


def receipt_refund_state(receipt: FeeReceipt) -> dict:
    """Refund status of a receipt, computed at read time (the snapshot never changes)."""
    refunds = FeeRefund.query.filter_by(payment_id=receipt.payment_id).order_by(FeeRefund.created_at.asc()).all()
    processed = sum((q2(r.amount) for r in refunds if r.status == "PROCESSED"), ZERO)
    paid = Decimal(str(receipt.snapshot.get("amount_paid", 0)))
    state = "NONE"
    if processed > 0:
        state = "FULL" if processed >= paid else "PARTIAL"
    return {
        "refund_status": state,
        "refunded_amount": money(processed),
        "refunds": [serialize_refund(r) for r in refunds],
    }


def serialize_receipt_summary(receipt: FeeReceipt) -> dict:
    snap = receipt.snapshot or {}
    refund = receipt_refund_state(receipt)
    return {
        "id": str(receipt.id),
        "receipt_number": receipt.receipt_number,
        "issued_at": iso(receipt.issued_at),
        "is_test": receipt.is_test,
        "amount": snap.get("amount_paid"),
        "payment_id": str(receipt.payment_id),
        "payment_number": (snap.get("payment") or {}).get("payment_number"),
        "invoice_id": str(receipt.invoice_id),
        "invoice_number": (snap.get("invoice") or {}).get("invoice_number"),
        "student_id": str(receipt.student_id),
        "student_name": (snap.get("student") or {}).get("name"),
        "refund_status": refund["refund_status"],
        "refunded_amount": refund["refunded_amount"],
    }


def serialize_receipt(receipt: FeeReceipt) -> dict:
    data = serialize_receipt_summary(receipt)
    refund = receipt_refund_state(receipt)
    data["refunds"] = refund["refunds"]
    data["snapshot"] = receipt.snapshot
    return data


def receipt_for_payment(payment_id):
    return FeeReceipt.query.filter_by(payment_id=payment_id).first()


def serialize_payment_row(p: FeePayment, inv: FeeInvoice = None, receipt: FeeReceipt = None) -> dict:
    inv = inv or p.invoice
    receipt = receipt if receipt is not None else receipt_for_payment(p.id)
    return {
        "id": str(p.id),
        "payment_number": p.payment_number,
        "invoice_id": str(p.invoice_id),
        "invoice_number": inv.invoice_number if inv else None,
        "amount": money(p.amount),
        "refunded_amount": money(p.refunded_amount),
        "method": p.payment_method,
        "status": "SUCCESS" if p.status == "COMPLETED" else p.status,
        "is_test": bool(p.is_test),
        "paid_at": iso(p.payment_date or p.created_at),
        "reference": p.transaction_reference,
        "order_id": str(p.order_id) if p.order_id else None,
        "receipt_id": str(receipt.id) if receipt else None,
        "receipt_number": receipt.receipt_number if receipt else None,
    }
