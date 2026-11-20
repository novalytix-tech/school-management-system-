import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity
from app.extensions import db
from app.models import (
    FeeStructure, FeeStructureItem, FeeInvoice, FeePayment, FinancialTransaction,
    AcademicYear, ClassLevel,
)
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException

finance_bp = Blueprint("finance", __name__, url_prefix="/finance")

# Numeric(12, 2) upper bound.
_MAX_MONEY = Decimal("9999999999.99")
_CENT = Decimal("0.01")
# Invoices in these states can no longer accept payments.
NON_PAYABLE_STATUSES = {"PAID", "CANCELLED", "CANCELED", "VOID", "WAIVED", "REFUNDED"}


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
        amount = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise APIException(f"{field} must be a finite number", status_code=400, error_code="INVALID_AMOUNT")
    if amount < 0 or (amount == 0 and not allow_zero):
        qualifier = "zero or more" if allow_zero else "greater than zero"
        raise APIException(f"{field} must be {qualifier}", status_code=400, error_code="INVALID_AMOUNT")
    if amount > _MAX_MONEY:
        raise APIException(f"{field} is too large", status_code=400, error_code="INVALID_AMOUNT")
    return amount


def new_payment_number() -> str:
    """Collision-free receipt number (a timestamp alone collides within one second)."""
    return f"PAY-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


def record_payment(invoice_id, institution_id, amount: Decimal, method: str,
                   reference=None, recorded_by=None, payment_number=None):
    """
    Apply a payment to an invoice under a row lock and write the ledger entry.

    The invoice row is locked (SELECT ... FOR UPDATE) for the whole
    read-modify-write of paid/balance so concurrent payments serialize instead
    of overwriting each other. Returns (invoice, payment); the caller commits.
    """
    inv = (
        FeeInvoice.query
        .filter_by(id=invoice_id, institution_id=institution_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not inv:
        raise APIException("Invoice not found", status_code=404)
    if (inv.status or "").upper() in NON_PAYABLE_STATUSES or inv.balance_amount <= 0:
        raise APIException(f"Invoice is {inv.status} and cannot accept payments",
                           status_code=409, error_code="INVOICE_NOT_PAYABLE")
    if amount > inv.balance_amount:
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
    )
    db.session.add(payment)
    # Assign payment.id now so the ledger row can reference it.
    db.session.flush()

    inv.paid_amount += amount
    inv.balance_amount -= amount
    inv.status = "PAID" if inv.balance_amount == 0 else "PARTIALLY_PAID"

    db.session.add(FinancialTransaction(
        institution_id=inv.institution_id,
        payment_id=payment.id,
        transaction_type="FEE_PAYMENT",
        category="REVENUE",
        amount=amount,
        credit_or_debit="CREDIT",
        description=f"Fee collection for {inv.invoice_number} via {method}",
    ))
    db.session.flush()
    return inv, payment


def _current_user_uuid():
    try:
        identity = get_jwt_identity()
        return uuid.UUID(str(identity)) if identity else None
    except Exception:
        return None


def _serialize_invoice(inv: FeeInvoice):
    student_name = "Student"
    adm_no = ""
    if inv.student:
        adm_no = inv.student.admission_number
        if inv.student.user:
            student_name = f"{inv.student.user.first_name} {inv.student.user.last_name}".strip()

    return {
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
            }
            for p in inv.payments
        ]
    }


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
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {
            "payment_id": str(payment.id),
            "payment_number": payment.payment_number,
            "invoice_status": inv.status,
            "paid_amount": float(inv.paid_amount),
            "remaining_balance": float(inv.balance_amount),
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

