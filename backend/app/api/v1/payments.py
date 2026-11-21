"""
Online fee payment endpoints (Razorpay Checkout + an explicit mock gateway).

Thin HTTP layer over app.services.payment_service — see docs/PAYMENTS_API.md.
The browser never decides that money was paid: an order only succeeds after a
valid signature AND a captured payment fetched from Razorpay (or a verified
webhook), or through the mock gateway when PAYMENT_MODE=mock.
"""
import hashlib
import json

from flask import Blueprint, request, jsonify, current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import PaymentGatewayWebhook
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException, DatabaseUnavailableError
from app.services import payment_service as ps
from app.services.fee_service import parse_uuid, current_user_uuid

payments_bp = Blueprint("payments", __name__, url_prefix="/payments")


def _require_db():
    if not current_app.config.get("DATABASE_ENABLED"):
        raise DatabaseUnavailableError()


def _json_body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    return payload


def _gateway_id(value, field):
    if not isinstance(value, str) or not ps.GATEWAY_ID_RE.match(value.strip()):
        raise APIException(f"{field} is missing or malformed", status_code=400, error_code="INVALID_FIELDS")
    return value.strip()


def _result(order):
    return jsonify({"success": True, "data": ps.order_result(order)}), ps.result_status_code(order)


@payments_bp.route("/config", methods=["GET"])
def payment_config():
    """Payment mode and the public Key ID (never the secret)."""
    institution_id = None
    if current_app.config.get("DATABASE_ENABLED"):
        institution_id = get_current_tenant_id()
    else:
        cfg = {"mode": ps.resolve_payment_mode()}
        cfg.update({"is_test": ps.mode_is_test(cfg["mode"]), "key_id": None, "currency": "INR",
                    "school_name": None, "merchant_name": None})
        if cfg["mode"] == "live":
            cfg["key_id"] = ps.razorpay_keys()[0]
        return jsonify({"success": True, "data": cfg}), 200
    return jsonify({"success": True, "data": ps.payment_config(institution_id)}), 200


@payments_bp.route("/razorpay/config", methods=["GET"])
def razorpay_config():
    """Legacy: whether Razorpay (live mode) is configured, plus the public Key ID."""
    live = ps.resolve_payment_mode() == "live"
    key_id = ps.razorpay_keys()[0] if live else ""
    return jsonify({"success": True, "data": {"configured": live, "keyId": key_id}}), 200


@payments_bp.route("/razorpay/order", methods=["POST"])
def create_order():
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    body = _json_body()
    if body.get("invoice_id") in (None, ""):
        raise APIException("invoice_id is required", status_code=400, error_code="INVOICE_REQUIRED")
    invoice_id = parse_uuid(body.get("invoice_id"), "invoice_id")
    currency = body.get("currency", "INR")
    if currency != "INR":
        raise APIException("Only INR payments are supported", status_code=400, error_code="INVALID_CURRENCY")
    payload, status = ps.create_checkout_order(institution_id, invoice_id, body.get("amount"),
                                               body.get("idempotency_key"))
    return jsonify({"success": True, "data": payload}), status


@payments_bp.route("/razorpay/verify", methods=["POST"])
def verify_payment():
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    body = _json_body()
    order_id = parse_uuid(body.get("order_id"), "order_id")
    gw_order = _gateway_id(body.get("razorpay_order_id"), "razorpay_order_id")
    gw_payment = _gateway_id(body.get("razorpay_payment_id"), "razorpay_payment_id")
    signature = body.get("razorpay_signature")
    if not isinstance(signature, str) or not signature or len(signature) > 256:
        raise APIException("razorpay_signature is missing or malformed", status_code=400,
                           error_code="INVALID_FIELDS")
    order = ps.verify_checkout(institution_id, order_id, gw_order, gw_payment, signature)
    return _result(order)


@payments_bp.route("/orders/<uuid:order_id>", methods=["GET"])
def get_order(order_id):
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    order = ps.load_order_for_caller(order_id, institution_id)
    return jsonify({"success": True, "data": ps.order_result(order)}), 200


@payments_bp.route("/orders/<uuid:order_id>/cancel", methods=["POST"])
def cancel_order(order_id):
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    order = ps.load_order_for_caller(order_id, institution_id)
    order = ps.cancel_order(order.id, current_user_uuid())
    return jsonify({"success": True, "data": ps.order_result(order)}), 200


@payments_bp.route("/orders/<uuid:order_id>/fail", methods=["POST"])
def fail_order(order_id):
    _require_db()
    institution_id = get_current_tenant_id(required=True)
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise APIException("reason must be a string", status_code=400)
    reason = (reason or "").strip()[:255] or "Payment failed at checkout"
    order = ps.load_order_for_caller(order_id, institution_id)
    order = ps.fail_order(order.id, reason, current_user_uuid())
    return jsonify({"success": True, "data": ps.order_result(order)}), 200


@payments_bp.route("/mock/orders/<uuid:order_id>/complete", methods=["POST"])
def mock_complete_order(order_id):
    _require_db()
    if ps.resolve_payment_mode() != "mock":
        raise APIException("Not found", status_code=404, error_code="NOT_FOUND")
    institution_id = get_current_tenant_id(required=True)
    body = _json_body()
    outcome = body.get("outcome")
    if outcome not in ("success", "failure", "cancel"):
        raise APIException("outcome must be success, failure or cancel", status_code=400,
                           error_code="INVALID_OUTCOME")
    order = ps.mock_complete(institution_id, order_id, outcome)
    return _result(order)


@payments_bp.route("/razorpay/webhook", methods=["POST"])
def razorpay_webhook():
    """Razorpay webhook (public): HMAC over the raw body, deduplicated by event id."""
    _require_db()
    raw = request.get_data(cache=True) or b""
    if len(raw) > 1024 * 1024:
        raise APIException("Payload too large", status_code=400, error_code="INVALID_PAYLOAD")
    if not ps.verify_webhook_signature(raw, request.headers.get("X-Razorpay-Signature")):
        current_app.logger.warning("Rejected Razorpay webhook with an invalid signature")
        raise APIException("Invalid webhook signature", status_code=400, error_code="SIGNATURE_MISMATCH")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise APIException("Malformed webhook payload", status_code=400, error_code="INVALID_PAYLOAD")
    if not isinstance(body, dict):
        raise APIException("Malformed webhook payload", status_code=400, error_code="INVALID_PAYLOAD")
    event = body.get("event") if isinstance(body.get("event"), str) else "unknown"
    event_id = (request.headers.get("X-Razorpay-Event-Id") or "").strip()[:100] \
        or "sha256:" + hashlib.sha256(raw).hexdigest()[:90]

    if PaymentGatewayWebhook.query.filter_by(event_id=event_id).first():
        return jsonify({"success": True, "data": {"duplicate": True, "event": event}}), 200

    outcome = ps.process_webhook(event, body)

    # Minimal log (ids and status only — no payer contact or instrument details).
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    pay = ((payload.get("payment") or {}).get("entity") or {}) if isinstance(payload.get("payment"), dict) else {}
    ref = ((payload.get("refund") or {}).get("entity") or {}) if isinstance(payload.get("refund"), dict) else {}
    summary = {"event": event, "outcome": outcome, "payment_id": pay.get("id") or ref.get("payment_id"),
               "order_id": pay.get("order_id"), "refund_id": ref.get("id"),
               "status": pay.get("status") or ref.get("status"), "amount": pay.get("amount") or ref.get("amount")}
    db.session.add(PaymentGatewayWebhook(gateway_provider="RAZORPAY", event_id=event_id, event_type=event[:100],
                                         payload_text=json.dumps(summary, default=str), is_verified=True))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # a concurrent delivery of the same event; processing is idempotent
        return jsonify({"success": True, "data": {"duplicate": True, "event": event}}), 200
    return jsonify({"success": True, "data": {"event": event, "outcome": outcome}}), 200
