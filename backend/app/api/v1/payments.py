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
import time
import urllib.error
import urllib.request

from flask import Blueprint, request, jsonify, current_app

payments_bp = Blueprint("payments", __name__, url_prefix="/payments")

RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"


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

    body = request.get_json(silent=True) or {}
    try:
        amount = int(body.get("amount") or 0)  # amount is in paise
    except (TypeError, ValueError):
        amount = 0
    if amount < 100:
        return jsonify({"success": False, "error": {"code": "INVALID_AMOUNT",
                       "message": "Amount must be at least 100 paise (₹1)."}}), 400

    payload = {
        "amount": amount,
        "currency": (body.get("currency") or "INR"),
        "receipt": body.get("receipt") or f"rcpt_{int(time.time())}",
        "notes": body.get("notes") or {},
    }

    auth = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    req = urllib.request.Request(
        RAZORPAY_ORDERS_URL,
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {auth}")

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

    body = request.get_json(silent=True) or {}
    order_id = body.get("razorpay_order_id") or body.get("orderId")
    payment_id = body.get("razorpay_payment_id") or body.get("paymentId")
    signature = body.get("razorpay_signature") or body.get("signature")

    if not (order_id and payment_id and signature):
        return jsonify({"success": False, "data": {"verified": False},
                        "error": {"code": "MISSING_FIELDS",
                                  "message": "order_id, payment_id and signature are required."}}), 400

    expected = hmac.new(
        key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    verified = hmac.compare_digest(expected, str(signature))

    if not verified:
        # Signature mismatch — reject with 400 and DO NOT mark the fee as paid.
        return jsonify({"success": False, "data": {"verified": False},
                        "error": {"code": "SIGNATURE_MISMATCH",
                                  "message": "Payment signature verification failed."}}), 400

    return jsonify({"success": True, "data": {"verified": True, "paymentId": payment_id}}), 200
