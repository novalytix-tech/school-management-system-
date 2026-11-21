import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from flask import Blueprint, request, jsonify, current_app, Response
from sqlalchemy import or_
from app.extensions import db
from app.models import (
    FeeStructure, FeeStructureItem, FeeInvoice, FeeInvoiceItem, FeePayment, FinancialTransaction,
    AcademicYear, ClassLevel, Section, Student, StudentEnrollment, User,
    FeeInvoiceAdjustment, FeeInstallment, FeeReceipt, FeeRefund, PaymentOrder,
)
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException, DatabaseUnavailableError
from app.services.security_audit import record_security_event
from app.services import fee_service as fees
# Re-exported for existing importers (payments, tests).
from app.services.fee_service import (  # noqa: F401
    NON_PAYABLE_STATUSES, parse_uuid, parse_money, new_payment_number, record_payment,
    current_user_uuid as _current_user_uuid,
)

finance_bp = Blueprint("finance", __name__, url_prefix="/finance")

_MAX_MONEY = fees.MAX_MONEY
_CENT = fees.CENT


def _require_db():
    if not current_app.config.get("DATABASE_ENABLED"):
        raise DatabaseUnavailableError()


def _json_body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    return payload


def _serialize_invoice(inv: FeeInvoice):
    student_name = "Student"
    adm_no = ""
    if inv.student:
        adm_no = inv.student.admission_number
        if inv.student.user:
            student_name = f"{inv.student.user.first_name} {inv.student.user.last_name}".strip()

    data = fees.serialize_portal_invoice(inv)
    data.update({
        "id": str(inv.id),
        "invoice_number": inv.invoice_number,
        "student_id": str(inv.student_id),
        "student_name": student_name,
        "admission_number": adm_no,
        "fee_structure_id": str(inv.fee_structure_id),
        "fee_structure_name": inv.fee_structure.name if inv.fee_structure else None,
        "subtotal": float(inv.subtotal),
        "discount_amount": float(inv.discount_amount),
        "total_amount": float(inv.total_amount),
        "paid_amount": float(inv.paid_amount),
        "balance_amount": float(inv.balance_amount),
        "status": inv.status,
        "issue_date": inv.issue_date.isoformat(),
        "due_date": inv.due_date.isoformat(),
        "items": [
            {
                "id": str(item.id),
                "fee_head": item.fee_head,
                "amount": float(item.amount),
            }
            for item in inv.items
        ],
        "payments": [
            {
                "id": str(p.id),
                "payment_number": p.payment_number,
                "amount": float(p.amount),
                "payment_method": p.payment_method,
                "payment_date": p.created_at.isoformat() if p.created_at else None,
                "refunded_amount": float(p.refunded_amount or 0),
                "status": "SUCCESS" if p.status == "COMPLETED" else p.status,
                "is_test": bool(p.is_test),
            }
            for p in inv.payments
        ]
    })
    return data


# -------------------------------------------------------------
# 1. Fee Structures
# -------------------------------------------------------------

@finance_bp.route("/fee-structures", methods=["GET"])
def list_fee_structures():
    institution_id = get_current_tenant_id()
    query = FeeStructure.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    structures = query.all()
    data = [
        {
            "id": str(fs.id),
            "name": fs.name,
            "academic_year_id": str(fs.academic_year_id),
            "academic_year_name": fs.academic_year.name if fs.academic_year else None,
            "class_level_id": str(fs.class_level_id),
            "class_name": fs.class_level.name if fs.class_level else None,
            "term_frequency": fs.term_frequency,
            "due_date": fs.due_date.isoformat(),
            "total_fee": float(sum(item.amount for item in fs.items)),
            "items": [
                {
                    "id": str(item.id),
                    "fee_head": item.fee_head,
                    "amount": float(item.amount),
                    "is_optional": item.is_optional,
                }
                for item in fs.items
            ]
        }
        for fs in structures
    ]
    return jsonify({"success": True, "data": data}), 200


@finance_bp.route("/fee-structures", methods=["POST"])
def create_fee_structure():
    institution_id = get_current_tenant_id(required=True)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)

    name = payload.get("name")
    academic_year_id = payload.get("academic_year_id")
    class_level_id = payload.get("class_level_id")

    if not name or not academic_year_id or not class_level_id:
        raise APIException("name, academic_year_id, and class_level_id are required", status_code=400)
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise APIException("name must be a non-empty string (max 100 characters)", status_code=400)

    year_uuid = parse_uuid(academic_year_id, "academic_year_id")
    class_uuid = parse_uuid(class_level_id, "class_level_id")
    if not AcademicYear.query.filter_by(id=year_uuid, institution_id=institution_id).first():
        raise APIException("Academic year not found", status_code=404)
    if not ClassLevel.query.filter_by(id=class_uuid, institution_id=institution_id).first():
        raise APIException("Class not found", status_code=404)

    # Idempotent check
    existing = FeeStructure.query.filter_by(
        institution_id=institution_id,
        academic_year_id=year_uuid,
        class_level_id=class_uuid,
        name=name.strip()
    ).first()
    if existing:
        return jsonify({
            "success": True,
            "data": {"id": str(existing.id), "name": existing.name},
            "message": f"Fee structure '{existing.name}' retrieved."
        }), 200

    due_date = payload.get("due_date")
    if due_date:
        try:
            d_date = datetime.strptime(str(due_date)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            raise APIException("due_date must be a date in YYYY-MM-DD format", status_code=400)
    else:
        d_date = date(2026, 12, 31)

    term_frequency = payload.get("term_frequency", "ANNUAL")
    if not isinstance(term_frequency, str) or not term_frequency.strip() or len(term_frequency) > 30:
        raise APIException("term_frequency must be a non-empty string", status_code=400)

    items = payload.get("items") or [{"fee_head": "Tuition Fee", "amount": 10000.0, "is_optional": False}]
    if not isinstance(items, list):
        raise APIException("items must be a list", status_code=400)
    parsed_items = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise APIException(f"items[{idx}] must be an object", status_code=400)
        fee_head = item.get("fee_head", "Tuition Fee")
        if not isinstance(fee_head, str) or not fee_head.strip() or len(fee_head.strip()) > 100:
            raise APIException(f"items[{idx}].fee_head must be a non-empty string", status_code=400)
        is_optional = item.get("is_optional", False)
        if not isinstance(is_optional, bool):
            raise APIException(f"items[{idx}].is_optional must be a boolean", status_code=400)
        amount = parse_money(item.get("amount", 10000.0), f"items[{idx}].amount", allow_zero=True)
        parsed_items.append((fee_head.strip(), amount, is_optional))

    fs = FeeStructure(
        institution_id=institution_id,
        academic_year_id=year_uuid,
        class_level_id=class_uuid,
        name=name.strip(),
        term_frequency=term_frequency.strip().upper(),
        due_date=d_date,
    )
    db.session.add(fs)
    db.session.flush()

    for fee_head, amount, is_optional in parsed_items:
        db.session.add(FeeStructureItem(
            fee_structure_id=fs.id,
            fee_head=fee_head,
            amount=amount,
            is_optional=is_optional,
        ))

    db.session.commit()

    return jsonify({
        "success": True,
        "data": {"id": str(fs.id), "name": fs.name},
        "message": f"Fee structure '{fs.name}' created successfully."
    }), 201


# -------------------------------------------------------------
# 2. Invoices
# -------------------------------------------------------------

@finance_bp.route("/invoices", methods=["GET"])
def list_invoices():
    institution_id = get_current_tenant_id()
    query = FeeInvoice.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    student_id = request.args.get("student_id")
    if student_id:
        query = query.filter_by(student_id=parse_uuid(student_id, "student_id"))

    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status.upper())

    invoices = query.order_by(FeeInvoice.due_date.asc()).all()
    return jsonify({
        "success": True,
        "data": [_serialize_invoice(inv) for inv in invoices]
    }), 200


@finance_bp.route("/invoices/<uuid:invoice_id>", methods=["GET"])
def get_invoice(invoice_id):
    institution_id = get_current_tenant_id(required=True)
    inv = FeeInvoice.query.filter_by(id=invoice_id, institution_id=institution_id).first()
    if not inv:
        raise APIException("Invoice not found", status_code=404)

    return jsonify({"success": True, "data": _serialize_invoice(inv)}), 200


@finance_bp.route("/invoices/<uuid:invoice_id>/pay", methods=["POST"])
def record_invoice_payment(invoice_id):
    """Process a payment against an invoice and record ledger transaction."""
    institution_id = get_current_tenant_id(required=True)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)

    raw_amount = payload.get("amount")
    if raw_amount is None or raw_amount == "":
        raise APIException("Payment amount is required", status_code=400)
    payment_amt = parse_money(raw_amount, "amount")

    method = payload.get("payment_method", "CASH")
    if not isinstance(method, str) or not method.strip() or len(method.strip()) > 30:
        raise APIException("payment_method must be a non-empty string", status_code=400)
    method = method.strip().upper()

    reference = payload.get("transaction_reference")
    if reference is not None and (not isinstance(reference, str) or len(reference) > 100):
        raise APIException("transaction_reference must be a string (max 100 characters)", status_code=400)

    inv, payment = record_payment(
        invoice_id, institution_id, payment_amt, method,
        reference=reference, recorded_by=_current_user_uuid(),
    )
    receipt = fees.receipt_for_payment(payment.id)
    db.session.commit()
    _emit_fee_change(institution_id, {"invoice_id": str(inv.id), "student_id": str(inv.student_id),
                                      "payment_id": str(payment.id), "status": "SUCCEEDED"})

    return jsonify({
        "success": True,
        "data": {
            "payment_id": str(payment.id),
            "payment_number": payment.payment_number,
            "invoice_status": inv.status,
            "paid_amount": float(inv.paid_amount),
            "remaining_balance": float(inv.balance_amount),
            "receipt_id": str(receipt.id) if receipt else None,
            "receipt_number": receipt.receipt_number if receipt else None,
            "invoice": fees.invoice_balance_fields(inv),
        },
        "message": f"Payment of {payment_amt} recorded successfully."
    }), 201


# -------------------------------------------------------------
# 3. Financial Summary
# -------------------------------------------------------------

@finance_bp.route("/summary", methods=["GET"])
def get_finance_summary():
    institution_id = get_current_tenant_id()
    query = FeeInvoice.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    invoices = query.all()
    total_billed = sum(i.total_amount for i in invoices)
    total_collected = sum(i.paid_amount for i in invoices)
    total_outstanding = sum(i.balance_amount for i in invoices)
    overdue_count = sum(1 for i in invoices if i.status != "PAID" and i.due_date < date.today())

    return jsonify({
        "success": True,
        "data": {
            "total_billed": float(total_billed),
            "total_collected": float(total_collected),
            "total_outstanding": float(total_outstanding),
            "overdue_invoices_count": overdue_count,
            "collection_rate": round(float(total_collected / total_billed * 100), 1) if total_billed > 0 else 0.0,
        }
    }), 200



# -------------------------------------------------------------
# 4. Student / parent fee portal
# -------------------------------------------------------------

def _emit_fee_change(institution_id, payload):
    try:
        from app.services.realtime_service import emit_entity_change
        emit_entity_change(institution_id, "fee_payment", "updated", payload)
    except Exception:  # pragma: no cover - best effort
        current_app.logger.warning("realtime emit failed", exc_info=True)


def _student_fees(student: Student) -> dict:
    from app.services.payment_service import timeline_entry
    invoices = (FeeInvoice.query.filter(FeeInvoice.student_id == student.id, FeeInvoice.is_deleted.is_(False))
                .order_by(FeeInvoice.due_date.asc(), FeeInvoice.created_at.asc()).all())
    summary = {k: fees.ZERO for k in ("total_assigned", "discounts", "scholarships", "fines", "paid",
                                       "refunded", "pending", "overdue", "remaining_balance")}
    inv_rows = []
    for inv in invoices:
        frozen = (inv.status or "").upper() in fees.FROZEN_STATUSES
        if not frozen:
            summary["total_assigned"] += fees.q2(inv.subtotal)
            sch = sum((fees.q2(a.amount) for a in inv.adjustments if a.kind == "SCHOLARSHIP"), fees.ZERO)
            summary["scholarships"] += sch
            summary["discounts"] += fees.q2(inv.discount_amount) - sch
            summary["fines"] += fees.q2(inv.fine_amount)
            balance = max(fees.q2(inv.balance_amount), fees.ZERO)
            overdue = fees.overdue_amount(inv)
            summary["overdue"] += overdue
            summary["pending"] += balance - overdue
            summary["remaining_balance"] += balance
        summary["paid"] += fees.q2(inv.paid_amount)
        summary["refunded"] += fees.q2(inv.refunded_amount)
        inv_rows.append(fees.serialize_portal_invoice(inv))
    inv_by_id = {inv.id: inv for inv in invoices}
    orders = (PaymentOrder.query.filter(PaymentOrder.student_id == student.id)
              .order_by(PaymentOrder.created_at.desc()).limit(200).all())
    payments = (FeePayment.query.join(FeeInvoice, FeePayment.invoice_id == FeeInvoice.id)
                .filter(FeeInvoice.student_id == student.id)
                .order_by(FeePayment.payment_date.desc()).all())
    return {
        "student": fees.student_profile(student),
        "summary": {k: fees.money(v) for k, v in summary.items()},
        "invoices": inv_rows,
        "timeline": [timeline_entry(o, inv_by_id.get(o.invoice_id)) for o in orders],
        "payments": [fees.serialize_payment_row(p, inv_by_id.get(p.invoice_id)) for p in payments],
    }


@finance_bp.route("/my-fees", methods=["GET"])
def my_fees():
    """Fees, payments and receipts of the caller's own record / wards (staff: one student)."""
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    students = fees.visible_students(institution_id, request.args.get("student_id"))
    return jsonify({"success": True, "data": {"students": [_student_fees(s) for s in students]}}), 200


# -------------------------------------------------------------
# 5. Receipts
# -------------------------------------------------------------

def _accessible_receipt(receipt_id, institution_id) -> FeeReceipt:
    receipt = FeeReceipt.query.filter_by(id=receipt_id, institution_id=institution_id).first()
    if receipt is None:
        raise APIException("Receipt not found", status_code=404, error_code="RECEIPT_NOT_FOUND")
    try:
        fees.accessible_student(receipt.student_id, institution_id)
    except APIException:
        raise APIException("Receipt not found", status_code=404, error_code="RECEIPT_NOT_FOUND")
    return receipt


@finance_bp.route("/receipts", methods=["GET"])
def list_receipts():
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    students = fees.visible_students(institution_id, request.args.get("student_id"), require_for_staff=False)
    query = FeeReceipt.query.filter(FeeReceipt.institution_id == institution_id)
    if students is not None:
        query = query.filter(FeeReceipt.student_id.in_([s.id for s in students] or [uuid.uuid4()]))
    receipts = query.order_by(FeeReceipt.issued_at.desc()).limit(500).all()
    items = [fees.serialize_receipt_summary(r) for r in receipts]
    return jsonify({"success": True, "data": {"items": items, "total": len(items)}}), 200


@finance_bp.route("/receipts/<uuid:receipt_id>", methods=["GET"])
def get_receipt(receipt_id):
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    receipt = _accessible_receipt(receipt_id, institution_id)
    return jsonify({"success": True, "data": fees.serialize_receipt(receipt)}), 200


@finance_bp.route("/receipts/<uuid:receipt_id>/pdf", methods=["GET"])
def get_receipt_pdf(receipt_id):
    _require_db()
    from app.services.receipt_pdf import render_receipt_pdf
    institution_id = get_current_tenant_id(required=True)
    receipt = _accessible_receipt(receipt_id, institution_id)
    pdf = render_receipt_pdf(receipt.snapshot, fees.receipt_refund_state(receipt))
    response = Response(pdf, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f"inline; filename={receipt.receipt_number}.pdf"
    response.headers["Cache-Control"] = "no-store"
    return response


# -------------------------------------------------------------
# 6. Invoice administration
# -------------------------------------------------------------

def _parse_adjustment(raw, field="adjustment"):
    if not isinstance(raw, dict):
        raise APIException(f"{field} must be an object", status_code=400)
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind.strip().upper() not in FeeInvoiceAdjustment.KINDS:
        raise APIException(f"{field}.kind must be one of {', '.join(FeeInvoiceAdjustment.KINDS)}",
                           status_code=400, error_code="INVALID_ADJUSTMENT")
    kind = kind.strip().upper()
    label = fees.parse_label(raw.get("label") or kind.replace("_", " ").title(), f"{field}.label", 150)
    amount = parse_money(raw.get("amount"), f"{field}.amount")
    return kind, label, amount


def _ensure_opening_discount(inv: FeeInvoice, actor):
    """Carry a pre-existing (legacy) discount into the adjustment ledger before recomputing."""
    has_reducing = any(a.kind in FeeInvoiceAdjustment.REDUCING_KINDS for a in inv.adjustments)
    if not has_reducing and fees.q2(inv.discount_amount) > 0:
        inv.adjustments.append(FeeInvoiceAdjustment(kind="DISCOUNT", label="Opening discount",
                                                    amount=fees.q2(inv.discount_amount), created_by=actor))


def _create_invoice(institution_id, student: Student, fs: FeeStructure, actor, *, due_date=None,
                    installments=None, installment_count=None, adjustments=None) -> FeeInvoice:
    subtotal = sum((fees.q2(i.amount) for i in fs.items), fees.ZERO)
    inv = FeeInvoice(
        institution_id=institution_id, invoice_number=fees.new_invoice_number(), student_id=student.id,
        fee_structure_id=fs.id, subtotal=subtotal, discount_amount=fees.ZERO, adjustment_amount=fees.ZERO,
        fine_amount=fees.ZERO, refunded_amount=fees.ZERO, total_amount=subtotal, paid_amount=fees.ZERO,
        balance_amount=subtotal, status="UNPAID", issue_date=fees.today_local(),
        due_date=due_date or fs.due_date,
    )
    inv.fee_structure = fs
    inv.student = student
    for item in fs.items:
        inv.items.append(FeeInvoiceItem(fee_head=item.fee_head, amount=fees.q2(item.amount)))
    for kind, label, amount in adjustments or []:
        inv.adjustments.append(FeeInvoiceAdjustment(kind=kind, label=label, amount=amount, created_by=actor))
    fees.recompute_invoice_totals(inv)
    if inv.total_amount <= 0:
        raise APIException("The invoice total must be greater than zero", status_code=400,
                           error_code="INVALID_INVOICE")
    fees.build_installments(inv, installments=installments, installment_count=installment_count)
    inv.status = fees.compute_invoice_status(inv)
    db.session.add(inv)
    db.session.flush()
    record_security_event(
        "INVOICE_CREATED", inv.id, entity_type="FeeInvoice", actor_user_id=actor, institution_id=institution_id,
        summary=f"{inv.invoice_number} for {student.admission_number}: total {inv.total_amount} in "
                f"{len(inv.installments)} installment(s).",
    )
    return inv


def _existing_invoice(student_id, fs_id):
    return FeeInvoice.query.filter(
        FeeInvoice.student_id == student_id, FeeInvoice.fee_structure_id == fs_id,
        FeeInvoice.is_deleted.is_(False),
        FeeInvoice.status.notin_(("CANCELLED", "CANCELED", "VOID")),
    ).first()


def _load_structure(fs_id, institution_id) -> FeeStructure:
    fs = FeeStructure.query.filter_by(id=fs_id, institution_id=institution_id).first()
    if fs is None or fs.is_deleted:
        raise APIException("Fee structure not found", status_code=404, error_code="FEE_STRUCTURE_NOT_FOUND")
    return fs


def _optional_count(payload):
    count = payload.get("installment_count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 12):
        raise APIException("installment_count must be an integer between 1 and 12", status_code=400)
    return count


@finance_bp.route("/invoices", methods=["POST"])
def create_invoice():
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    payload = _json_body()
    student_id = parse_uuid(payload.get("student_id"), "student_id")
    fs_id = parse_uuid(payload.get("fee_structure_id"), "fee_structure_id")
    due_date = fees.parse_date(payload["due_date"], "due_date") if payload.get("due_date") else None
    installments = payload.get("installments")
    if installments is not None and not isinstance(installments, list):
        raise APIException("installments must be a list", status_code=400)
    count = _optional_count(payload)
    raw_adj = payload.get("adjustments") or []
    if not isinstance(raw_adj, list) or len(raw_adj) > 20:
        raise APIException("adjustments must be a list (max 20)", status_code=400)
    adjustments = [_parse_adjustment(a, f"adjustments[{i}]") for i, a in enumerate(raw_adj)]

    student = Student.query.filter(Student.id == student_id, Student.institution_id == institution_id,
                                   Student.is_deleted.is_(False)).first()
    if student is None:
        raise APIException("Student not found", status_code=404, error_code="STUDENT_NOT_FOUND")
    fs = _load_structure(fs_id, institution_id)
    if _existing_invoice(student.id, fs.id):
        raise APIException("This student already has an invoice for this fee structure", status_code=409,
                           error_code="INVOICE_EXISTS")
    inv = _create_invoice(institution_id, student, fs, _current_user_uuid(), due_date=due_date,
                          installments=installments or None, installment_count=count, adjustments=adjustments)
    db.session.commit()
    _emit_fee_change(institution_id, {"invoice_id": str(inv.id), "student_id": str(student.id),
                                      "status": "INVOICE_CREATED"})
    return jsonify({"success": True, "data": _serialize_invoice(inv)}), 201


@finance_bp.route("/fee-structures/<uuid:fee_structure_id>/generate-invoices", methods=["POST"])
def generate_invoices(fee_structure_id):
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    count = _optional_count(payload)
    due_date = fees.parse_date(payload["due_date"], "due_date") if payload.get("due_date") else None
    fs = _load_structure(fee_structure_id, institution_id)
    sections = Section.query.filter(Section.class_level_id == fs.class_level_id)
    if payload.get("section_id"):
        section_id = parse_uuid(payload.get("section_id"), "section_id")
        sections = sections.filter(Section.id == section_id)
        if sections.first() is None:
            raise APIException("Section not found in this class", status_code=404, error_code="SECTION_NOT_FOUND")
    section_ids = [s.id for s in sections.all()]
    students = (Student.query.join(StudentEnrollment, StudentEnrollment.student_id == Student.id)
                .filter(Student.institution_id == institution_id, Student.is_deleted.is_(False),
                        StudentEnrollment.academic_year_id == fs.academic_year_id,
                        StudentEnrollment.section_id.in_(section_ids or [uuid.uuid4()]),
                        StudentEnrollment.status == "ACTIVE", StudentEnrollment.is_deleted.is_(False))
                .order_by(Student.admission_number.asc()).all())
    created = skipped = 0
    actor = _current_user_uuid()
    for student in students:
        if _existing_invoice(student.id, fs.id):
            skipped += 1
            continue
        _create_invoice(institution_id, student, fs, actor, due_date=due_date, installment_count=count)
        created += 1
    db.session.commit()
    if created:
        _emit_fee_change(institution_id, {"fee_structure_id": str(fs.id), "status": "INVOICES_GENERATED",
                                          "count": created})
    return jsonify({"success": True, "data": {"created": created, "skipped": skipped}}), 200


def _adjust_and_respond(inv: FeeInvoice, institution_id, summary):
    fees.recompute_invoice_totals(inv)
    record_security_event("INVOICE_ADJUSTED", inv.id, entity_type="FeeInvoice",
                          actor_user_id=_current_user_uuid(), institution_id=institution_id,
                          summary=f"{inv.invoice_number}: {summary}; total {inv.total_amount}, "
                                  f"balance {inv.balance_amount}.")
    db.session.commit()
    _emit_fee_change(institution_id, {"invoice_id": str(inv.id), "student_id": str(inv.student_id),
                                      "status": "INVOICE_ADJUSTED"})
    return jsonify({"success": True, "data": _serialize_invoice(inv)}), 200


def _lock_adjustable_invoice(invoice_id, institution_id) -> FeeInvoice:
    inv = fees.lock_invoice(invoice_id, institution_id)
    if (inv.status or "").upper() in fees.FROZEN_STATUSES:
        raise APIException(f"Invoice is {inv.status} and cannot be adjusted", status_code=409,
                           error_code="INVOICE_NOT_ADJUSTABLE")
    return inv


@finance_bp.route("/invoices/<uuid:invoice_id>/adjustments", methods=["POST"])
def add_invoice_adjustment(invoice_id):
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    kind, label, amount = _parse_adjustment(_json_body())
    inv = _lock_adjustable_invoice(invoice_id, institution_id)
    actor = _current_user_uuid()
    _ensure_opening_discount(inv, actor)
    inv.adjustments.append(FeeInvoiceAdjustment(kind=kind, label=label, amount=amount, created_by=actor))
    return _adjust_and_respond(inv, institution_id, f"added {kind} '{label}' {amount}")


@finance_bp.route("/invoices/<uuid:invoice_id>/adjustments/<uuid:adjustment_id>", methods=["DELETE"])
def delete_invoice_adjustment(invoice_id, adjustment_id):
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    inv = _lock_adjustable_invoice(invoice_id, institution_id)
    adj = next((a for a in inv.adjustments if a.id == adjustment_id), None)
    if adj is None:
        raise APIException("Adjustment not found", status_code=404, error_code="ADJUSTMENT_NOT_FOUND")
    inv.adjustments.remove(adj)
    return _adjust_and_respond(inv, institution_id, f"removed {adj.kind} '{adj.label}' {adj.amount}")


# -------------------------------------------------------------
# 7. Transaction history & refunds (administration)
# -------------------------------------------------------------

PAYMENT_STATUSES = {"SUCCESS", "PARTIALLY_REFUNDED", "REFUNDED"}
ORDER_STATUSES = set(PaymentOrder.STATUSES)


def _student_names(student_ids) -> dict:
    if not student_ids:
        return {}
    rows = (db.session.query(Student.id, User.first_name, User.last_name)
            .outerjoin(User, Student.user_id == User.id).filter(Student.id.in_(list(student_ids))).all())
    return {sid: (f"{fn or ''} {ln or ''}".strip() or "Student") for sid, fn, ln in rows}


@finance_bp.route("/payments", methods=["GET"])
def list_payments():
    _require_db()
    from app.services.payment_service import timeline_entry
    institution_id = get_current_tenant_id(required=True)
    args = request.args
    try:
        page = max(int(args.get("page", 1)), 1)
        limit = min(max(int(args.get("limit", 25)), 1), 100)
    except (TypeError, ValueError):
        raise APIException("page and limit must be integers", status_code=400)
    status = (args.get("status") or "").strip().upper() or None
    if status and status not in PAYMENT_STATUSES | ORDER_STATUSES:
        raise APIException("Unknown status filter", status_code=400, error_code="INVALID_STATUS")
    start = fees.parse_date(args["from"], "from") if args.get("from") else None
    end = fees.parse_date(args["to"], "to") if args.get("to") else None
    student_id = parse_uuid(args["student_id"], "student_id") if args.get("student_id") else None

    def _bound(d, plus_day=False):
        d = d + timedelta(days=1) if plus_day else d
        return datetime(d.year, d.month, d.day, tzinfo=fees.IST)

    rows = []
    if status is None or status in PAYMENT_STATUSES:
        pq = (db.session.query(FeePayment, FeeInvoice)
              .join(FeeInvoice, FeePayment.invoice_id == FeeInvoice.id)
              .filter(FeeInvoice.institution_id == institution_id))
        if student_id:
            pq = pq.filter(FeeInvoice.student_id == student_id)
        if status == "SUCCESS":
            pq = pq.filter(FeePayment.status.in_(("SUCCESS", "COMPLETED")))
        elif status:
            pq = pq.filter(FeePayment.status == status)
        if start:
            pq = pq.filter(FeePayment.payment_date >= _bound(start))
        if end:
            pq = pq.filter(FeePayment.payment_date < _bound(end, True))
        for p, inv in pq.order_by(FeePayment.payment_date.desc()).limit(5000).all():
            row = fees.serialize_payment_row(p, inv)
            row.update({
                "kind": "payment",
                "student_id": str(inv.student_id),
                "refundable_amount": fees.money(fees.refundable_amount(p)),
                "created_at": fees.iso(p.created_at),
                "provider": None,
                "refunds": [fees.serialize_refund(r) for r in p.refunds],
                "_sort": p.payment_date or p.created_at,
                "_sid": inv.student_id,
            })
            rows.append(row)
    if status is None or status in ORDER_STATUSES:
        oq = PaymentOrder.query.filter(PaymentOrder.institution_id == institution_id)
        if status:
            oq = oq.filter(PaymentOrder.status == status)
        else:
            # Settled orders are listed as their payment.
            oq = oq.filter(PaymentOrder.status != "SUCCEEDED")
        if student_id:
            oq = oq.filter(PaymentOrder.student_id == student_id)
        if start:
            oq = oq.filter(PaymentOrder.created_at >= _bound(start))
        if end:
            oq = oq.filter(PaymentOrder.created_at < _bound(end, True))
        for o in oq.order_by(PaymentOrder.created_at.desc()).limit(5000).all():
            entry = timeline_entry(o)
            entry.update({
                "id": str(o.id),
                "kind": "order",
                "student_id": str(o.student_id),
                "payment_number": None,
                "refunded_amount": 0.0,
                "refundable_amount": 0.0,
                "method": "RAZORPAY" if o.provider == "RAZORPAY" else "ONLINE_MOCK",
                "paid_at": entry.get("succeeded_at"),
                "created_at": fees.iso(o.created_at),
                "_sort": o.created_at,
                "_sid": o.student_id,
            })
            rows.append(entry)
    rows.sort(key=lambda r: r["_sort"], reverse=True)
    total = len(rows)
    page_rows = rows[(page - 1) * limit: page * limit]
    names = _student_names({r["_sid"] for r in page_rows})
    for r in page_rows:
        r["student_name"] = names.get(r.pop("_sid"))
        r.pop("_sort", None)
    return jsonify({"success": True, "data": {
        "items": page_rows, "total": total, "page": page, "limit": limit,
        "pages": max((total + limit - 1) // limit, 1),
    }}), 200


@finance_bp.route("/payments/<uuid:payment_id>/refund", methods=["POST"])
def refund_payment(payment_id):
    _require_db()
    from app.services.payment_service import request_refund
    institution_id = get_current_tenant_id(required=True)
    payload = _json_body()
    raw_amount = payload.get("amount")
    if raw_amount is None or raw_amount == "":
        raise APIException("amount is required", status_code=400, error_code="INVALID_AMOUNT")
    amount = parse_money(raw_amount, "amount")
    reason = fees.parse_label(payload.get("reason"), "reason", 255)
    refund = request_refund(institution_id, payment_id, amount, reason, _current_user_uuid())
    payment = db.session.get(FeePayment, refund.payment_id)
    inv = db.session.get(FeeInvoice, payment.invoice_id)
    return jsonify({"success": True, "data": {
        "refund": fees.serialize_refund(refund),
        "payment": {**fees.serialize_payment_row(payment, inv),
                    "refundable_amount": fees.money(fees.refundable_amount(payment))},
        "invoice": fees.invoice_balance_fields(inv),
    }}), (201 if refund.status == "PROCESSED" else 202)
