"""
Online fee payments: payment mode, Razorpay REST client, checkout orders,
settlement, webhooks and refunds.

Only the Python standard library is used for the gateway (urllib + hmac). The
Key Secret and webhook secret never leave the server. Every network call goes
through ``_rzp_request`` so tests can mock a single seam.

Settlement (``settle_order``) is the ONE idempotent function used by checkout
verification, webhooks and mock completion: it locks the order row, takes the
amount from server truth (the Razorpay payment fetched with the secret key, or
the order row in mock mode), records the payment and issues the receipt, all in
one transaction. A receipt is never created for an unverified payment.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from decimal import Decimal

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import FeeInvoice, FeePayment, FeeRefund, FeeReceipt, PaymentOrder, Institution, User, Student
from app.utils.errors import APIException
from app.services.security_audit import record_security_event
from app.services.realtime_service import emit_entity_change
from app.services import fee_service as fees
from app.services.fee_service import q2, money, iso, now_utc, ZERO

logger = logging.getLogger("payments")

RAZORPAY_API = "https://api.razorpay.com/v1"
RAZORPAY_PAYMENT_METHOD = "RAZORPAY"
MOCK_PAYMENT_METHOD = "ONLINE_MOCK"
MIN_ORDER_AMOUNT = Decimal("1.00")
GATEWAY_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
OPEN_ORDER_STATUSES = ("CREATED", "PENDING")
SETTLEABLE_STATUSES = ("CREATED", "PENDING", "FAILED", "CANCELLED", "EXPIRED")


class GatewayError(Exception):
    """Razorpay could not be reached or rejected the request."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Mode & configuration
# ---------------------------------------------------------------------------

def _cfg(name):
    value = current_app.config.get(name)
    return value.strip() if isinstance(value, str) else value


def razorpay_keys():
    return (_cfg("RAZORPAY_KEY_ID") or "", _cfg("RAZORPAY_KEY_SECRET") or "")


def resolve_payment_mode() -> str:
    """live | mock | disabled, from PAYMENT_MODE (auto → live when both keys are set)."""
    mode = (_cfg("PAYMENT_MODE") or "auto").lower()
    key_id, key_secret = razorpay_keys()
    if mode == "mock":
        return "mock"
    if mode == "disabled":
        return "disabled"
    if mode in ("live", "auto"):
        return "live" if (key_id and key_secret) else "disabled"
    return "disabled"


def live_is_test_key() -> bool:
    key_id, _ = razorpay_keys()
    return key_id.startswith("rzp_test_")


def mode_is_test(mode: str) -> bool:
    return mode == "mock" or (mode == "live" and live_is_test_key())


def payment_config(institution_id) -> dict:
    mode = resolve_payment_mode()
    key_id, _ = razorpay_keys()
    inst = db.session.get(Institution, institution_id) if institution_id else None
    school = inst.name if inst else "School"
    return {
        "mode": mode,
        "is_test": mode_is_test(mode),
        "key_id": key_id if mode == "live" else None,
        "currency": "INR",
        "school_name": school,
        "merchant_name": school,
    }


# ---------------------------------------------------------------------------
# Razorpay REST client (the only network seam)
# ---------------------------------------------------------------------------

def _basic_auth(key_id, key_secret):
    return "Basic " + base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()


def _rzp_request(method: str, path: str, payload=None) -> dict:
    key_id, key_secret = razorpay_keys()
    if not key_id or not key_secret:
        raise GatewayError("Razorpay keys are not configured", code="PAYMENTS_DISABLED")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{RAZORPAY_API}{path}", data=data, method=method)
    req.add_header("Authorization", _basic_auth(key_id, key_secret))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:500]
        except Exception:
            pass
        logger.warning("Razorpay %s %s failed (%s): %s", method, path.split("?")[0], e.code, detail)
        raise GatewayError("Razorpay rejected the request", status=e.code,
                           code="RAZORPAY_AUTH_FAILED" if e.code in (401, 403) else "RAZORPAY_ERROR")
    except Exception as e:  # network / timeout / bad JSON
        logger.warning("Razorpay %s %s error: %s", method, path.split("?")[0], e.__class__.__name__)
        raise GatewayError("Could not reach Razorpay", code="RAZORPAY_UNREACHABLE")
    if not isinstance(body, dict):
        raise GatewayError("Unexpected Razorpay response", code="RAZORPAY_ERROR")
    return body


def rzp_create_order(amount_paise: int, receipt: str, notes: dict) -> dict:
    return _rzp_request("POST", "/orders", {
        "amount": amount_paise, "currency": "INR", "receipt": receipt[:40], "notes": notes,
    })


def rzp_fetch_payment(payment_id: str) -> dict:
    return _rzp_request("GET", f"/payments/{payment_id}")


def rzp_create_refund(payment_id: str, amount_paise: int, notes: dict) -> dict:
    return _rzp_request("POST", f"/payments/{payment_id}/refund", {"amount": amount_paise, "notes": notes})


def _gateway_exception(err: GatewayError) -> APIException:
    if err.code == "PAYMENTS_DISABLED":
        return APIException("Online payment is not configured", status_code=409, error_code="PAYMENTS_DISABLED")
    if err.code == "RAZORPAY_AUTH_FAILED":
        return APIException("The payment gateway rejected the server credentials. Please contact the school.",
                            status_code=502, error_code="RAZORPAY_AUTH_FAILED")
    return APIException("The payment gateway is unavailable. Please try again.", status_code=502,
                        error_code=err.code or "RAZORPAY_UNREACHABLE")


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

def verify_checkout_signature(gateway_order_id: str, gateway_payment_id: str, signature: str) -> bool:
    _, key_secret = razorpay_keys()
    if not key_secret or not isinstance(signature, str):
        return False
    expected = hmac.new(key_secret.encode(), f"{gateway_order_id}|{gateway_payment_id}".encode(),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def verify_webhook_signature(raw_body: bytes, signature) -> bool:
    secret = _cfg("RAZORPAY_WEBHOOK_SECRET") or ""
    if not secret or not isinstance(signature, str) or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


# ---------------------------------------------------------------------------
# Order serialization
# ---------------------------------------------------------------------------

def order_result(order: PaymentOrder, *, message=None) -> dict:
    """SettlementResult shape shared by verify / cancel / fail / get / mock-complete."""
    receipt = FeeReceipt.query.filter_by(payment_id=order.payment_id).first() if order.payment_id else None
    inv = db.session.get(FeeInvoice, order.invoice_id)
    data = {
        "status": order.status,
        "order_id": str(order.id),
        "invoice_id": str(order.invoice_id),
        "amount": money(order.amount),
        "amount_paise": fees.to_paise(order.amount),
        "currency": order.currency,
        "provider": order.provider,
        "is_test": order.is_test,
        "gateway_order_id": order.gateway_order_id,
        "gateway_payment_id": order.gateway_payment_id,
        "failure_reason": order.failure_reason,
        "payment_id": str(order.payment_id) if order.payment_id else None,
        "receipt_id": str(receipt.id) if receipt else None,
        "receipt_number": receipt.receipt_number if receipt else None,
        "initiated_at": iso(order.created_at),
        "pending_at": iso(order.pending_at),
        "succeeded_at": iso(order.succeeded_at),
        "failed_at": iso(order.failed_at),
        "cancelled_at": iso(order.cancelled_at),
        "invoice": fees.invoice_balance_fields(inv) if inv else None,
    }
    if message:
        data["message"] = message
    return data


def result_status_code(order: PaymentOrder) -> int:
    return 202 if order.status in OPEN_ORDER_STATUSES else 200


def timeline_entry(order: PaymentOrder, inv: FeeInvoice = None) -> dict:
    inv = inv or db.session.get(FeeInvoice, order.invoice_id)
    receipt = FeeReceipt.query.filter_by(payment_id=order.payment_id).first() if order.payment_id else None
    refunds = (FeeRefund.query.filter_by(payment_id=order.payment_id).order_by(FeeRefund.created_at.asc()).all()
               if order.payment_id else [])
    return {
        "order_id": str(order.id),
        "invoice_id": str(order.invoice_id),
        "invoice_number": inv.invoice_number if inv else None,
        "amount": money(order.amount),
        "status": order.status,
        "provider": order.provider,
        "is_test": order.is_test,
        "gateway_order_id": order.gateway_order_id,
        "gateway_payment_id": order.gateway_payment_id,
        "failure_reason": order.failure_reason,
        "initiated_at": iso(order.created_at),
        "pending_at": iso(order.pending_at),
        "succeeded_at": iso(order.succeeded_at),
        "failed_at": iso(order.failed_at),
        "cancelled_at": iso(order.cancelled_at),
        "payment_id": str(order.payment_id) if order.payment_id else None,
        "receipt_id": str(receipt.id) if receipt else None,
        "receipt_number": receipt.receipt_number if receipt else None,
        "refunds": [
            {"id": str(r.id), "amount": money(r.amount), "status": r.status,
             "created_at": iso(r.created_at), "processed_at": iso(r.processed_at)}
            for r in refunds
        ],
    }


def _emit(order_or_inst, payload):
    try:
        emit_entity_change(order_or_inst, "fee_payment", "updated", payload)
    except Exception:  # pragma: no cover - realtime is best-effort
        logger.warning("realtime emit failed", exc_info=True)


def _order_event_payload(order: PaymentOrder) -> dict:
    return {
        "order_id": str(order.id), "invoice_id": str(order.invoice_id), "student_id": str(order.student_id),
        "status": order.status, "payment_id": str(order.payment_id) if order.payment_id else None,
    }


# ---------------------------------------------------------------------------
# Checkout orders
# ---------------------------------------------------------------------------

def load_order_for_caller(order_id, institution_id) -> PaymentOrder:
    """An order the caller started, or one for a student they may access (404 otherwise)."""
    order = PaymentOrder.query.filter_by(id=order_id, institution_id=institution_id).first()
    if order is None:
        raise APIException("Order not found", status_code=404, error_code="ORDER_NOT_FOUND")
    user_id = fees.current_user_uuid()
    if order.initiated_by and order.initiated_by == user_id:
        return order
    if fees.is_admin():
        return order
    if order.student_id in fees.own_student_ids(institution_id):
        return order
    raise APIException("Order not found", status_code=404, error_code="ORDER_NOT_FOUND")


def create_checkout_order(institution_id, invoice_id, amount_raw, idempotency_key) -> tuple:
    """Create (or return the idempotent duplicate of) a checkout order. Returns (payload, status)."""
    mode = resolve_payment_mode()
    if mode == "disabled":
        raise APIException("Online payment is not configured for this school", status_code=409,
                           error_code="PAYMENTS_DISABLED")
    if not isinstance(idempotency_key, str) or not IDEMPOTENCY_RE.match(idempotency_key.strip()):
        raise APIException("idempotency_key must be 8-128 characters (letters, digits, - _ : .)",
                           status_code=400, error_code="INVALID_IDEMPOTENCY_KEY")
    idempotency_key = idempotency_key.strip()
    user_id = fees.current_user_uuid()
    if user_id is None:
        raise APIException("Authentication required", status_code=401, error_code="UNAUTHORIZED")

    inv = FeeInvoice.query.filter_by(id=invoice_id, institution_id=institution_id, is_deleted=False).first()
    if inv is None:
        raise APIException("Invoice not found", status_code=404, error_code="INVOICE_NOT_FOUND")
    # Ownership: the student, a linked parent/guardian, or a school admin.
    student = fees.accessible_student(inv.student_id, institution_id, staff_roles=fees.ADMIN_ROLES)

    existing = PaymentOrder.query.filter_by(initiated_by=user_id, idempotency_key=idempotency_key).first()
    if existing is not None:
        return _idempotent_order(existing, inv), 200

    if not fees.is_payable(inv):
        raise APIException(f"Invoice is {inv.status} and cannot accept payments", status_code=409,
                           error_code="INVOICE_NOT_PAYABLE")
    if amount_raw is None or amount_raw == "":
        amount = fees.next_payable_amount(inv)
    else:
        amount = fees.parse_money(amount_raw, "amount")
    if amount < MIN_ORDER_AMOUNT:
        raise APIException("Amount must be at least Rs. 1.00", status_code=400, error_code="AMOUNT_TOO_SMALL")
    if amount > q2(inv.balance_amount):
        raise APIException(f"Amount cannot exceed the remaining balance ({q2(inv.balance_amount)})",
                           status_code=400, error_code="AMOUNT_EXCEEDS_BALANCE")

    provider = "MOCK" if mode == "mock" else "RAZORPAY"
    order = PaymentOrder(
        institution_id=institution_id, invoice_id=inv.id, student_id=inv.student_id, initiated_by=user_id,
        provider=provider, is_test=mode_is_test(mode), amount=amount, currency="INR", status="CREATED",
        idempotency_key=idempotency_key,
    )
    db.session.add(order)
    try:
        db.session.flush()  # claims the idempotency key before any gateway call
    except IntegrityError:
        db.session.rollback()
        existing = PaymentOrder.query.filter_by(initiated_by=user_id, idempotency_key=idempotency_key).first()
        if existing is None:
            raise
        return _idempotent_order(existing, inv), 200

    if provider == "MOCK":
        order.gateway_order_id = f"order_mock_{uuid.uuid4().hex[:20]}"
    else:
        db.session.commit()  # the reservation survives a gateway failure
        try:
            gw = rzp_create_order(fees.to_paise(amount), f"ord_{order.id.hex[:32]}", {
                "invoice_id": str(inv.id), "institution_id": str(institution_id), "order_id": str(order.id),
            })
        except GatewayError as err:
            order = db.session.get(PaymentOrder, order.id)
            order.status = "FAILED"
            order.failed_at = now_utc()
            order.failure_reason = "Payment gateway unavailable when creating the order"
            db.session.commit()
            raise _gateway_exception(err)
        gw_id = gw.get("id")
        if not isinstance(gw_id, str) or not GATEWAY_ID_RE.match(gw_id) \
                or gw.get("amount") != fees.to_paise(amount) or gw.get("currency", "INR") != "INR":
            order.status = "FAILED"
            order.failed_at = now_utc()
            order.failure_reason = "Unexpected response from the payment gateway"
            db.session.commit()
            raise APIException("The payment gateway returned an unexpected response", status_code=502,
                               error_code="RAZORPAY_ERROR")
        order.gateway_order_id = gw_id

    record_security_event(
        "PAYMENT_ORDER_CREATED", order.id, entity_type="PaymentOrder", actor_user_id=user_id,
        institution_id=institution_id,
        summary=f"{provider} order {order.gateway_order_id} for {amount} on {inv.invoice_number}"
                + (" (TEST)" if order.is_test else "") + ".",
    )
    db.session.commit()
    _emit(institution_id, _order_event_payload(order))
    return checkout_payload(order, inv, student), 201


def _idempotent_order(existing: PaymentOrder, inv: FeeInvoice) -> dict:
    if existing.invoice_id != inv.id:
        raise APIException("idempotency_key was already used for another invoice", status_code=409,
                           error_code="IDEMPOTENCY_KEY_REUSED")
    if existing.status not in OPEN_ORDER_STATUSES or not existing.gateway_order_id:
        raise APIException(f"idempotency_key belongs to an order that is {existing.status}; "
                           "start a new checkout", status_code=409, error_code="IDEMPOTENCY_KEY_REUSED")
    return checkout_payload(existing, inv, db.session.get(Student, existing.student_id))


def checkout_payload(order: PaymentOrder, inv: FeeInvoice, student: Student) -> dict:
    mode = "mock" if order.provider == "MOCK" else "live"
    key_id, _ = razorpay_keys()
    payer = db.session.get(User, order.initiated_by) if order.initiated_by else None
    student_name = "Student"
    if student is not None and student.user:
        student_name = f"{student.user.first_name} {student.user.last_name}".strip() or student_name
    return {
        "order_id": str(order.id),
        "gateway_order_id": order.gateway_order_id,
        "amount": money(order.amount),
        "amount_paise": fees.to_paise(order.amount),
        "currency": order.currency,
        "key_id": key_id if mode == "live" else None,
        "mode": mode,
        "is_test": order.is_test,
        "status": order.status,
        "invoice_id": str(inv.id),
        "invoice_number": inv.invoice_number,
        "student_name": student_name,
        "prefill": {
            "name": f"{payer.first_name} {payer.last_name}".strip() if payer else "",
            "email": payer.email if payer else "",
            "contact": (payer.phone or "") if payer else "",
        },
    }


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def _lock_order(order_id) -> PaymentOrder:
    return (PaymentOrder.query.filter_by(id=order_id)
            .populate_existing().with_for_update().one())


def cancel_order(order_id, actor=None) -> PaymentOrder:
    order = _lock_order(order_id)
    if order.status == "CANCELLED":
        db.session.commit()
        return order
    if order.status != "CREATED":
        db.session.rollback()
        raise APIException(f"Order is {order.status} and cannot be cancelled", status_code=409,
                           error_code="INVALID_ORDER_STATE")
    order.status = "CANCELLED"
    order.cancelled_at = now_utc()
    record_security_event("PAYMENT_CANCELLED", order.id, entity_type="PaymentOrder", actor_user_id=actor,
                          institution_id=order.institution_id,
                          summary=f"Checkout {order.gateway_order_id} closed by the payer.")
    db.session.commit()
    _emit(order.institution_id, _order_event_payload(order))
    return order


def fail_order(order_id, reason, actor=None, *, from_gateway=False) -> PaymentOrder:
    order = _lock_order(order_id)
    if order.status == "FAILED":
        db.session.commit()
        return order
    if order.status not in OPEN_ORDER_STATUSES:
        db.session.rollback()
        if from_gateway:
            return order
        raise APIException(f"Order is {order.status} and cannot be marked failed", status_code=409,
                           error_code="INVALID_ORDER_STATE")
    order.status = "FAILED"
    order.failed_at = now_utc()
    order.failure_reason = (reason or "Payment failed")[:255]
    record_security_event("PAYMENT_FAILED", order.id, entity_type="PaymentOrder", actor_user_id=actor,
                          institution_id=order.institution_id,
                          summary=f"Checkout {order.gateway_order_id} failed: {order.failure_reason}")
    db.session.commit()
    _emit(order.institution_id, _order_event_payload(order))
    return order


def _mark_needs_review(order: PaymentOrder, reason: str, gateway_payment_id=None):
    order.status = "NEEDS_REVIEW"
    order.failure_reason = reason[:255]
    logger.error("Payment order %s (%s, payment %s, amount %s) NEEDS REVIEW / refund: %s",
                 order.id, order.gateway_order_id, gateway_payment_id, order.amount, reason)
    record_security_event("PAYMENT_NEEDS_REVIEW", order.id, entity_type="PaymentOrder",
                          institution_id=order.institution_id,
                          summary=f"Payment {gateway_payment_id or '-'} on {order.gateway_order_id} "
                                  f"needs review (refund may be required): {reason}"[:1000])


def settle_order(order_id, gateway_payment_id: str, *, payment_entity: dict = None, source: str = "verify"):
    """
    Idempotently settle a checkout order. Returns the locked/updated order.

    live: the payment must exist at Razorpay (fetched with the secret key unless
    a signature-verified webhook entity is supplied), belong to this order, be
    in INR for exactly the order amount, and be ``captured``. ``authorized``
    leaves the order PENDING; ``failed`` marks it FAILED.
    mock: the amount is the order amount (no money moves; flagged as test).
    """
    order = db.session.get(PaymentOrder, order_id)
    if order is None:
        raise APIException("Order not found", status_code=404, error_code="ORDER_NOT_FOUND")
    if order.status == "SUCCEEDED":
        return order

    entity_status, instrument = "captured", None
    if order.provider == "RAZORPAY":
        entity = payment_entity
        if entity is None:
            try:
                entity = rzp_fetch_payment(gateway_payment_id)
            except GatewayError as err:
                raise _gateway_exception(err)
        if not isinstance(entity, dict) or entity.get("id") != gateway_payment_id \
                or entity.get("order_id") != order.gateway_order_id:
            raise APIException("Payment does not belong to this order", status_code=400,
                               error_code="ORDER_MISMATCH")
        entity_status = entity.get("status")
        instrument = entity.get("method") if isinstance(entity.get("method"), str) else None
        paise = entity.get("amount")
        amount_ok = (not isinstance(paise, bool) and isinstance(paise, int)
                     and paise == fees.to_paise(order.amount) and entity.get("currency", "INR") == "INR")
        failure = entity.get("error_description") or entity.get("error_reason") or "Payment failed"
    else:
        amount_ok, failure = True, None

    order = _lock_order(order.id)
    if order.status == "SUCCEEDED":
        if order.gateway_payment_id != gateway_payment_id:
            logger.error("Order %s already settled by %s; second payment %s needs review/refund",
                         order.id, order.gateway_payment_id, gateway_payment_id)
        db.session.commit()
        return order
    if order.status == "NEEDS_REVIEW":
        db.session.commit()
        return order
    if order.gateway_payment_id and order.gateway_payment_id != gateway_payment_id \
            and entity_status == "captured":
        # A different captured payment for the same order: never double count.
        _mark_needs_review(order, "Multiple payments captured for one order", gateway_payment_id)
        db.session.commit()
        return order

    if entity_status == "failed":
        db.session.rollback()
        return fail_order(order.id, str(failure), from_gateway=True)
    if entity_status in ("authorized", "created"):
        if order.status in ("CREATED", "PENDING", "FAILED", "CANCELLED", "EXPIRED"):
            order.status = "PENDING"
            order.pending_at = order.pending_at or now_utc()
            order.gateway_payment_id = gateway_payment_id
            order.failure_reason = None
        db.session.commit()
        _emit(order.institution_id, _order_event_payload(order))
        return order
    if entity_status != "captured":
        _mark_needs_review(order, f"Gateway reported payment status '{entity_status}'", gateway_payment_id)
        db.session.commit()
        return order
    if not amount_ok:
        order.gateway_payment_id = gateway_payment_id
        _mark_needs_review(order, "Captured amount/currency does not match the order", gateway_payment_id)
        db.session.commit()
        return order

    order.gateway_payment_id = gateway_payment_id
    is_mock = order.provider == "MOCK"
    savepoint = db.session.begin_nested()
    try:
        inv, payment = fees.record_payment(
            order.invoice_id, order.institution_id, q2(order.amount),
            MOCK_PAYMENT_METHOD if is_mock else RAZORPAY_PAYMENT_METHOD,
            reference=gateway_payment_id, recorded_by=order.initiated_by,
            payment_number=f"{'MOCK' if is_mock else 'RZP'}-{gateway_payment_id}",
            is_test=order.is_test, order=order, instrument=instrument,
        )
        savepoint.commit()
    except APIException as e:
        savepoint.rollback()
        order = _lock_order(order.id)
        order.gateway_payment_id = gateway_payment_id
        _mark_needs_review(order, f"Payment received but could not be applied: {e.message}", gateway_payment_id)
        db.session.commit()
        _emit(order.institution_id, _order_event_payload(order))
        return order

    order.status = "SUCCEEDED"
    order.succeeded_at = now_utc()
    order.failure_reason = None
    order.payment_id = payment.id
    order.pending_at = order.pending_at or order.succeeded_at
    record_security_event(
        "PAYMENT_SUCCEEDED", order.id, entity_type="PaymentOrder", actor_user_id=order.initiated_by,
        institution_id=order.institution_id,
        summary=f"{order.provider} payment {gateway_payment_id} of {order.amount} settled on "
                f"{inv.invoice_number} via {source}; balance {inv.balance_amount}"
                + (" (TEST)" if order.is_test else "") + ".",
    )
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent settlement claimed the gateway payment id / receipt.
        db.session.rollback()
        order = db.session.get(PaymentOrder, order_id)
        db.session.refresh(order)
        if order.status == "SUCCEEDED":
            return order
        raise
    _emit(order.institution_id, {**_order_event_payload(order), "invoice_status": inv.status,
                                 "balance_amount": money(inv.balance_amount)})
    return order


def verify_checkout(institution_id, order_id, gateway_order_id, gateway_payment_id, signature) -> PaymentOrder:
    order = load_order_for_caller(order_id, institution_id)
    if order.provider != "RAZORPAY":
        raise APIException("This order is not a Razorpay order", status_code=400, error_code="INVALID_PROVIDER")
    if resolve_payment_mode() != "live":
        raise APIException("Online payment is not configured", status_code=409, error_code="PAYMENTS_DISABLED")
    if gateway_order_id != order.gateway_order_id:
        raise APIException("Order does not match this checkout", status_code=400, error_code="ORDER_MISMATCH")
    if not verify_checkout_signature(gateway_order_id, gateway_payment_id, signature):
        logger.warning("Razorpay signature mismatch for order %s", order.id)
        raise APIException("Payment signature verification failed", status_code=400,
                           error_code="SIGNATURE_MISMATCH")
    return settle_order(order.id, gateway_payment_id, source="verify")


def mock_complete(institution_id, order_id, outcome: str) -> PaymentOrder:
    order = load_order_for_caller(order_id, institution_id)
    if order.provider != "MOCK":
        raise APIException("Order not found", status_code=404, error_code="ORDER_NOT_FOUND")
    actor = fees.current_user_uuid()
    if outcome == "success":
        pay_id = order.gateway_payment_id or f"pay_mock_{uuid.uuid4().hex[:20]}"
        return settle_order(order.id, pay_id, source="mock")
    if outcome == "failure":
        return fail_order(order.id, "Payment declined (simulated)", actor)
    return cancel_order(order.id, actor)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def request_refund(institution_id, payment_id, amount: Decimal, reason: str, actor) -> FeeRefund:
    """Refund part or all of a payment. Gateway refunds stay PENDING until processed."""
    payment = (FeePayment.query.join(FeeInvoice, FeePayment.invoice_id == FeeInvoice.id)
               .filter(FeePayment.id == payment_id, FeeInvoice.institution_id == institution_id)
               .populate_existing().with_for_update(of=FeePayment).first())
    if payment is None:
        raise APIException("Payment not found", status_code=404, error_code="PAYMENT_NOT_FOUND")
    refundable = fees.refundable_amount(payment)
    if amount > refundable:
        raise APIException(f"Refund cannot exceed the refundable amount ({refundable})", status_code=400,
                           error_code="OVER_REFUND")
    order = db.session.get(PaymentOrder, payment.order_id) if payment.order_id else None
    via_gateway = order is not None and order.provider == "RAZORPAY"
    if via_gateway and resolve_payment_mode() != "live":
        raise APIException("Gateway refunds need online payment to be configured", status_code=409,
                           error_code="PAYMENTS_DISABLED")

    refund = FeeRefund(payment_id=payment.id, amount=amount, reason=reason, status="PENDING", requested_by=actor)
    db.session.add(refund)
    db.session.flush()

    if not via_gateway:
        # Mock (simulated) and offline (cash returned) refunds complete immediately.
        fees.apply_processed_refund(refund, actor)
        db.session.commit()
        _emit(institution_id, {"refund_id": str(refund.id), "payment_id": str(payment.id),
                               "invoice_id": str(payment.invoice_id), "status": refund.status})
        return refund

    db.session.commit()  # reserve the amount before calling the gateway
    try:
        gw = rzp_create_refund(order.gateway_payment_id, fees.to_paise(amount), {
            "refund_id": str(refund.id), "payment_id": str(payment.id),
        })
    except GatewayError as err:
        refund = db.session.get(FeeRefund, refund.id)
        refund.status = "FAILED"
        refund.processed_at = now_utc()
        db.session.commit()
        raise _gateway_exception(err)
    refund = db.session.get(FeeRefund, refund.id)
    gw_id = gw.get("id")
    if isinstance(gw_id, str) and GATEWAY_ID_RE.match(gw_id):
        refund.gateway_refund_id = gw_id
    if gw.get("status") == "processed":
        fees.apply_processed_refund(refund, actor)
    else:
        record_security_event("REFUND_ISSUED", payment.id, entity_type="FeePayment", actor_user_id=actor,
                              institution_id=institution_id,
                              summary=f"Refund {amount} of {payment.payment_number} requested at Razorpay "
                                      f"({gw_id}); pending.")
    db.session.commit()
    _emit(institution_id, {"refund_id": str(refund.id), "payment_id": str(payment.id),
                           "invoice_id": str(payment.invoice_id), "status": refund.status})
    return refund


def _process_refund_event(event: str, entity: dict):
    gw_refund_id = entity.get("id")
    gw_payment_id = entity.get("payment_id")
    if not isinstance(gw_refund_id, str) or not isinstance(gw_payment_id, str):
        return "ignored"
    refund = FeeRefund.query.filter_by(gateway_refund_id=gw_refund_id).first()
    order = PaymentOrder.query.filter_by(gateway_payment_id=gw_payment_id).first()
    if refund is None:
        if order is None or order.payment_id is None:
            return "ignored"
        paise = entity.get("amount")
        if isinstance(paise, bool) or not isinstance(paise, int) or paise <= 0:
            return "ignored"
        amount = q2(Decimal(paise) / 100)
        # A pending refund we requested whose id was not stored, else one made on the dashboard.
        refund = (FeeRefund.query.filter_by(payment_id=order.payment_id, status="PENDING", amount=amount,
                                            gateway_refund_id=None).first())
        if refund is None:
            if event != "refund.processed":
                return "ignored"
            payment = db.session.get(FeePayment, order.payment_id)
            if amount > fees.refundable_amount(payment):
                logger.error("Razorpay refund %s exceeds the refundable amount of payment %s", gw_refund_id,
                             payment.id)
                return "ignored"
            refund = FeeRefund(payment_id=payment.id, amount=amount, status="PENDING",
                               reason="Refunded via Razorpay dashboard")
            db.session.add(refund)
        refund.gateway_refund_id = gw_refund_id
        db.session.flush()
    refund = FeeRefund.query.filter_by(id=refund.id).populate_existing().with_for_update().one()
    if event == "refund.processed" and refund.status == "PENDING":
        fees.apply_processed_refund(refund)
    elif event == "refund.failed" and refund.status == "PENDING":
        refund.status = "FAILED"
        refund.processed_at = now_utc()
        logger.error("Razorpay refund %s failed", gw_refund_id)
    db.session.commit()
    payment = db.session.get(FeePayment, refund.payment_id)
    inv = db.session.get(FeeInvoice, payment.invoice_id)
    _emit(inv.institution_id, {"refund_id": str(refund.id), "payment_id": str(payment.id),
                               "invoice_id": str(inv.id), "status": refund.status})
    return refund.status


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def process_webhook(event: str, body: dict) -> str:
    """Apply one verified webhook event. Returns a short outcome string."""
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if event in ("refund.processed", "refund.failed"):
        entity = ((payload.get("refund") or {}).get("entity")) if isinstance(payload.get("refund"), dict) else None
        return _process_refund_event(event, entity) if isinstance(entity, dict) else "ignored"

    if event not in ("payment.captured", "order.paid", "payment.authorized", "payment.failed"):
        return "ignored"
    entity = ((payload.get("payment") or {}).get("entity")) if isinstance(payload.get("payment"), dict) else None
    if not isinstance(entity, dict):
        return "ignored"
    gw_payment_id, gw_order_id = entity.get("id"), entity.get("order_id")
    if not (isinstance(gw_payment_id, str) and GATEWAY_ID_RE.match(gw_payment_id)
            and isinstance(gw_order_id, str)):
        return "ignored"
    order = PaymentOrder.query.filter_by(gateway_order_id=gw_order_id, provider="RAZORPAY").first()
    if order is None:
        logger.info("Webhook %s for unknown order %s ignored", event, gw_order_id)
        return "ignored"
    if event == "payment.failed":
        entity = {**entity, "status": "failed"}
    elif event == "payment.authorized":
        entity = {**entity, "status": entity.get("status") or "authorized"}
    try:
        order = settle_order(order.id, gw_payment_id, payment_entity=entity, source=f"webhook {event}")
    except APIException as e:
        db.session.rollback()
        logger.warning("Webhook %s for order %s not applied: %s", event, order.id, e.message)
        return "rejected"
    return order.status
