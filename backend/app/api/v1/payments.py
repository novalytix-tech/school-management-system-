"""
Razorpay payment endpoints for fee payments.

Uses only the Python standard library (urllib + hmac) so no extra package is
required. The Key Secret stays server-side; the browser only ever sees the
public Key ID. If the keys are not configured the endpoints return a clear
"not configured" response and the frontend falls back to its test checkout.
"""
import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request
from decimal import Decimal

from flask import Blueprint, request, jsonify, current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import FeeInvoice, FeePayment
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException
from app.api.v1.finance import (
    NON_PAYABLE_STATUSES, parse_uuid, record_payment, _current_user_uuid,
)

payments_bp = Blueprint("payments", __name__, url_prefix="/payments")

RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"
RAZORPAY_PAYMENTS_URL = "https://api.razorpay.com/v1/payments"
RAZORPAY_PAYMENT_METHOD = "RAZORPAY"
_RZP_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


def _basic_auth(key_id, key_secret):
    return "Basic " + base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()


def _fetch_order(order_id, key_id, key_secret):
    """Fetch an order from Razorpay (server truth for amount + invoice binding)."""
    req = urllib.request.Request(f"{RAZORPAY_ORDERS_URL}/{order_id}", method="GET")
    req.add_header("Authorization", _basic_auth(key_id, key_secret))
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _fetch_payment(payment_id, key_id, key_secret):
    """Fetch a payment from Razorpay (server truth for capture status + amount)."""
    req = urllib.request.Request(f"{RAZORPAY_PAYMENTS_URL}/{payment_id}", method="GET")
    req.add_header("Authorization", _basic_auth(key_id, key_secret))
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _parse_paise(value):
    """Amount in paise: a positive integer (no floats, no bools, no truncation)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _keys():
    return (
        (current_app.config.get("RAZORPAY_KEY_ID") or "").strip(),
        (current_app.config.get("RAZORPAY_KEY_SECRET") or "").strip(),
    )


@payments_bp.route("/razorpay/config", methods=["GET"])
def razorpay_config():
    """Tell the frontend whether Razorpay is configured and expose the public Key ID."""
    key_id, _ = _keys()
    return jsonify({"success": True, "data": {"configured": bool(key_id), "keyId": key_id}}), 200


@payments_bp.route("/razorpay/order", methods=["POST"])
def create_order():
    """Create a Razorpay order so the payment can be verified server-side."""
    key_id, key_secret = _keys()
    if not key_id or not key_secret:
        return (
            jsonify({"success": False, "error": {"code": "RAZORPAY_NOT_CONFIGURED",
                     "message": "Razorpay keys are not configured on the server."}}),
            200,
        )

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise APIException("Request body must be a JSON object", status_code=400)

    notes = body.get("notes") or {}
    if not isinstance(notes, dict):
        raise APIException("notes must be an object", status_code=400)

    # Every order is bound to a tenant-scoped invoice so verification can record it.
    invoice_raw = body.get("invoice_id") or notes.get("invoice_id")
    if not invoice_raw:
        raise APIException("invoice_id is required", status_code=400, error_code="INVOICE_REQUIRED")
    institution_id = get_current_tenant_id(required=True)
    invoice_id = parse_uuid(invoice_raw, "invoice_id")
    inv = FeeInvoice.query.filter_by(id=invoice_id, institution_id=institution_id).first()
    if not inv:
        raise APIException("Invoice not found", status_code=404)
    if (inv.status or "").upper() in NON_PAYABLE_STATUSES or inv.balance_amount <= 0:
        raise APIException(f"Invoice is {inv.status} and cannot accept payments",
                           status_code=409, error_code="INVOICE_NOT_PAYABLE")

    currency = body.get("currency") or "INR"
    if currency != "INR":
        return jsonify({"success": False, "error": {"code": "INVALID_CURRENCY",
                       "message": "Only INR payments are supported."}}), 400

    amount = _parse_paise(body.get("amount"))  # amount is in paise
    if amount is None or amount < 100:
        return jsonify({"success": False, "error": {"code": "INVALID_AMOUNT",
                       "message": "Amount must be an integer number of paise, at least 100 (\u20b91)."}}), 400
    balance_paise = int((inv.balance_amount * 100).to_integral_value())
    if amount > balance_paise:
        return jsonify({"success": False, "error": {"code": "AMOUNT_EXCEEDS_BALANCE",
                       "message": "Amount exceeds the invoice's remaining balance."}}), 400

    safe_notes = {str(k)[:40]: str(v)[:200] for k, v in list(notes.items())[:10]}
    safe_notes["invoice_id"] = str(inv.id)
    safe_notes["institution_id"] = str(institution_id)
    payload = {
        "amount": amount,
        "currency": "INR",
        "receipt": f"inv_{inv.id.hex}",
        "notes": safe_notes,
    }

    req = urllib.request.Request(
        RAZORPAY_ORDERS_URL,
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", _basic_auth(key_id, key_secret))

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            order = json.loads(resp.read().decode())
        return (
            jsonify({"success": True, "data": {
                "orderId": order.get("id"),
                "amount": order.get("amount"),
                "currency": order.get("currency"),
                "keyId": key_id,
            }}),
            200,
        )
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore") if hasattr(e, "read") else str(e)
        current_app.logger.warning("Razorpay order creation failed (%s): %s", e.code, detail)
        # Bad/expired API keys → 401; any other gateway error → 500.
        if e.code in (401, 403):
            return jsonify({"success": False, "error": {"code": "RAZORPAY_AUTH_FAILED",
                           "message": "Razorpay authentication failed. Check RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET."}}), 401
        return jsonify({"success": False, "error": {"code": "RAZORPAY_ORDER_FAILED",
                       "message": "Razorpay could not create the order. Please try again."}}), 500
    except Exception as e:  # network/timeouts
        current_app.logger.warning("Razorpay order creation error: %s", e)
        return jsonify({"success": False, "error": {"code": "RAZORPAY_UNREACHABLE",
                       "message": "Could not reach Razorpay. Please try again."}}), 500


@payments_bp.route("/razorpay/verify", methods=["POST"])
def verify_payment():
    """Verify the Razorpay payment signature (HMAC-SHA256 of 'order_id|payment_id')."""
    _, key_secret = _keys()
    if not key_secret:
        return (
            jsonify({"success": False, "error": {"code": "RAZORPAY_NOT_CONFIGURED",
                     "message": "Razorpay secret is not configured on the server."}}),
            200,
        )

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    order_id = body.get("razorpay_order_id") or body.get("orderId")
    payment_id = body.get("razorpay_payment_id") or body.get("paymentId")
    signature = body.get("razorpay_signature") or body.get("signature")

    if not (order_id and payment_id and signature):
        return jsonify({"success": False, "data": {"verified": False},
                        "error": {"code": "MISSING_FIELDS",
                                  "message": "order_id, payment_id and signature are required."}}), 400
    if not all(isinstance(v, str) and _RZP_ID_RE.match(v) for v in (order_id, payment_id)) \
            or not isinstance(signature, str):
        return jsonify({"success": False, "data": {"verified": False},
                        "error": {"code": "INVALID_FIELDS",
                                  "message": "order_id, payment_id and signature are malformed."}}), 400

    expected = hmac.new(
        key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    verified = hmac.compare_digest(expected, signature)

    if not verified:
        # Signature mismatch — reject with 400 and DO NOT mark the fee as paid.
        return jsonify({"success": False, "data": {"verified": False},
                        "error": {"code": "SIGNATURE_MISMATCH",
                                  "message": "Payment signature verification failed."}}), 400

    invoice_raw = body.get("invoice_id")
    if not invoice_raw:
        # Signature-only check (legacy client). Nothing is recorded server-side.
        return jsonify({"success": True, "data": {"verified": True, "paymentId": payment_id,
                                                  "recorded": False}}), 200

    return _record_razorpay_payment(invoice_raw, order_id, payment_id)


def _record_razorpay_payment(invoice_raw, order_id, payment_id):
    """
    Record a verified Razorpay payment against its invoice, server-side.

    The amount and the invoice binding come from the Razorpay order (fetched
    with the secret key), never from the client. Recording is idempotent on the
    Razorpay payment id: the receipt number is derived from it (and is UNIQUE),
    so a replayed signature returns the existing receipt instead of paying twice.
    """
    institution_id = get_current_tenant_id(required=True)
    invoice_id = parse_uuid(invoice_raw, "invoice_id")
    payment_number = f"RZP-{payment_id}"

    def _already_recorded():
        existing = FeePayment.query.filter_by(payment_number=payment_number).first()
        if not existing:
            return None
        if existing.invoice_id != invoice_id:
            raise APIException("This payment was already recorded against another invoice",
                               status_code=409, error_code="PAYMENT_ALREADY_RECORDED")
        return jsonify({"success": True, "data": {
            "verified": True, "paymentId": payment_id, "recorded": True, "duplicate": True,
            "payment_number": existing.payment_number,
            "invoice_id": str(existing.invoice_id),
        }}), 200

    dup = _already_recorded()
    if dup:
        return dup

    key_id, key_secret = _keys()
    try:
        order = _fetch_order(order_id, key_id, key_secret)
        rzp_payment = _fetch_payment(payment_id, key_id, key_secret)
    except Exception as e:  # network / gateway errors — client may retry (idempotent)
        current_app.logger.warning("Razorpay order/payment fetch failed: %s", e)
        return jsonify({"success": False, "data": {"verified": True, "recorded": False},
                        "error": {"code": "RAZORPAY_UNREACHABLE",
                                  "message": "Payment verified but could not be confirmed with Razorpay. Please retry."}}), 502

    notes = order.get("notes") if isinstance(order, dict) and isinstance(order.get("notes"), dict) else {}
    if order.get("id") != order_id or notes.get("invoice_id") != str(invoice_id) \
            or notes.get("institution_id") != str(institution_id):
        raise APIException("Order does not belong to this invoice", status_code=400, error_code="ORDER_MISMATCH")
    if order.get("currency") != "INR":
        raise APIException("Only INR payments are supported", status_code=400, error_code="INVALID_CURRENCY")
    if not isinstance(rzp_payment, dict) or rzp_payment.get("order_id") != order_id:
        raise APIException("Payment does not belong to this order", status_code=400, error_code="ORDER_MISMATCH")

    # Use the payment itself, not the order's amount_paid: the order lags behind
    # capture and stays 0 on manual-capture accounts.
    status = rzp_payment.get("status")
    if status == "authorized":
        # Money is held but not yet captured: retryable, nothing recorded yet.
        return jsonify({"success": False, "data": {"verified": True, "recorded": False},
                        "error": {"code": "PAYMENT_NOT_CAPTURED",
                                  "message": "Payment is authorized but not yet captured. Please retry shortly."}}), 409
    if status != "captured":
        raise APIException("Payment was not completed", status_code=409, error_code="PAYMENT_NOT_COMPLETED")
    paid_paise = rzp_payment.get("amount")
    if isinstance(paid_paise, bool) or not isinstance(paid_paise, int) or paid_paise <= 0:
        raise APIException("Payment amount is invalid", status_code=409, error_code="PAYMENT_NOT_CAPTURED")

    amount = (Decimal(paid_paise) / 100).quantize(Decimal("0.01"))
    try:
        inv, payment = record_payment(
            invoice_id, institution_id, amount, RAZORPAY_PAYMENT_METHOD,
            reference=payment_id, recorded_by=_current_user_uuid(), payment_number=payment_number,
        )
        db.session.commit()
    except APIException as e:
        # Captured money that can't be applied (overpayment, invoice already paid
        # by another channel). Never drop it silently: flag it for reconciliation.
        db.session.rollback()
        current_app.logger.error(
            "Razorpay payment %s (order %s, %s paise) captured but not recorded for invoice %s: %s — needs reconciliation/refund",
            payment_id, order_id, paid_paise, invoice_id, e.message,
        )
        return jsonify({"success": False, "data": {"verified": True, "recorded": False},
                        "error": {"code": "NEEDS_RECONCILIATION",
                                  "message": f"Payment received but could not be applied ({e.message}). "
                                             "The school office has been notified; please keep your payment ID."}}), 409
    except IntegrityError:
        # A concurrent verify of the same payment won the race (UNIQUE payment_number).
        db.session.rollback()
        dup = _already_recorded()
        if dup:
            return dup
        raise

    return jsonify({"success": True, "data": {
        "verified": True, "paymentId": payment_id, "recorded": True, "duplicate": False,
        "payment_number": payment.payment_number,
        "invoice_id": str(inv.id),
        "invoice_status": inv.status,
        "paid_amount": float(inv.paid_amount),
        "remaining_balance": float(inv.balance_amount),
    }}), 201
