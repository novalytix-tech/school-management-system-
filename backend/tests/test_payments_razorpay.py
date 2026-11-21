"""
Fee payments: checkout orders, Razorpay verify/webhooks (mocked HTTP), mock
gateway, installments, adjustments, refunds and receipts (docs/PAYMENTS_API.md).

Opt-in (needs TEST_DATABASE_URL). No network traffic: urllib is blocked and the
Razorpay helpers in app.services.payment_service are replaced per test. Every
test removes the fee structures/invoices/payments it created.
"""
import hashlib
import hmac
import json
import re
import uuid
import zlib
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    Institution, User, Role, UserRole, Student, Parent, StudentParent, StudentEnrollment, Section,
    FeeStructure, FeeStructureItem, FeeInvoice, FeePayment, FinancialTransaction, FeeReceipt, FeeRefund,
    PaymentOrder, PaymentGatewayWebhook, FeeInstallment, TokenBlocklist, AuditLog,
)
from app.services import payment_service as ps
from app.utils.security import hash_password
from tests._auth import admin_headers, bearer_for

PRINCIPAL = "principal@stxaviers.edu.in"
TEACHER = "ananya.sharma@stxaviers.edu.in"
PARENT = "vikram.patel@gmail.com"
STUDENT_A = "aarav.sharma@student.stxaviers.edu.in"
STUDENT_B = "diya.patel@student.stxaviers.edu.in"
OTHER_PARENT = "other.parent.paytest@example.test"
KEY_ID, KEY_SECRET, WEBHOOK_SECRET = "rzp_test_paytest123", "paytest-key-secret", "paytest-webhook-secret"
RCT_RE = re.compile(r"^RCT-\d{4}-\d{6}$")


# ---------------------------------------------------------------- fixtures

def _inst():
    return Institution.query.filter_by(code="STXAV-DELHI").first()


def _student(email):
    return Student.query.join(User, Student.user_id == User.id).filter(User.email == email).first()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*a, **k):
        raise AssertionError("Unexpected network call to Razorpay")
    monkeypatch.setattr(ps.urllib.request, "urlopen", _blocked)


@pytest.fixture
def mode(app, monkeypatch):
    """Switch the payment mode for one test (keys/secret are fake test values)."""
    def _set(value, key_id=KEY_ID, key_secret=KEY_SECRET):
        monkeypatch.setitem(app.config, "PAYMENT_MODE", value)
        monkeypatch.setitem(app.config, "RAZORPAY_KEY_ID", key_id if value in ("live", "auto") else "")
        monkeypatch.setitem(app.config, "RAZORPAY_KEY_SECRET", key_secret if value in ("live", "auto") else "")
        monkeypatch.setitem(app.config, "RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    return _set


@pytest.fixture
def env(app):
    """A fresh fee structure (Tuition 6000 + Lab 2000 + Sports 1000) and a second parent."""
    inst = _inst()
    a, b = _student(STUDENT_A), _student(STUDENT_B)
    # Diya's enrollment is in the seeded session for the whole class (other suites
    # may give Aarav an extra enrollment in a new session).
    enrollment = StudentEnrollment.query.filter_by(student_id=b.id, status="ACTIVE").first()
    section = db.session.get(Section, enrollment.section_id)
    fs = FeeStructure(institution_id=inst.id, academic_year_id=enrollment.academic_year_id,
                      class_level_id=section.class_level_id, name=f"PayTest {uuid.uuid4().hex[:8]}",
                      term_frequency="ANNUAL", due_date=date.today() + timedelta(days=60))
    db.session.add(fs)
    db.session.flush()
    for head, amount in (("Tuition Fee", "6000.00"), ("Lab Fee", "2000.00"), ("Sports Fee", "1000.00")):
        db.session.add(FeeStructureItem(fee_structure_id=fs.id, fee_head=head, amount=Decimal(amount)))
    user = User(institution_id=inst.id, username=f"paytest_parent_{uuid.uuid4().hex[:6]}", email=OTHER_PARENT,
                password_hash=hash_password("SchoolERP@Demo2026!"), first_name="Other", last_name="Parent",
                is_active=True)
    db.session.add(user)
    db.session.flush()
    db.session.add(UserRole(user_id=user.id, role_id=Role.query.filter_by(name="parent").first().id))
    parent = Parent(institution_id=inst.id, user_id=user.id, first_name="Other", last_name="Parent",
                    relationship_type="Guardian", phone="+91 90000 00000")
    db.session.add(parent)
    db.session.flush()
    db.session.add(StudentParent(student_id=b.id, parent_id=parent.id))
    db.session.commit()
    ctx = {"inst_id": inst.id, "fs_id": fs.id, "a_id": a.id, "b_id": b.id, "section_id": section.id,
           "parent_user": user.id, "parent_id": parent.id, "structures": [fs.id]}
    yield ctx
    _purge(ctx["structures"])
    StudentParent.query.filter_by(parent_id=ctx["parent_id"]).delete()
    Parent.query.filter_by(id=ctx["parent_id"]).delete()
    UserRole.query.filter_by(user_id=ctx["parent_user"]).delete()
    TokenBlocklist.query.filter_by(user_id=ctx["parent_user"]).delete()
    AuditLog.query.filter_by(actor_user_id=ctx["parent_user"]).delete()
    User.query.filter_by(id=ctx["parent_user"]).delete()
    PaymentGatewayWebhook.query.filter(PaymentGatewayWebhook.event_id.like("evt_paytest_%")).delete(
        synchronize_session=False)
    db.session.commit()


def _purge(fs_ids):
    db.session.rollback()
    inv_ids = [i for (i,) in db.session.query(FeeInvoice.id).filter(FeeInvoice.fee_structure_id.in_(fs_ids))]
    if inv_ids:
        pay_ids = [p for (p,) in db.session.query(FeePayment.id).filter(FeePayment.invoice_id.in_(inv_ids))]
        FeeReceipt.query.filter(FeeReceipt.invoice_id.in_(inv_ids)).delete(synchronize_session=False)
        if pay_ids:
            FeeRefund.query.filter(FeeRefund.payment_id.in_(pay_ids)).delete(synchronize_session=False)
            FinancialTransaction.query.filter(FinancialTransaction.payment_id.in_(pay_ids)).delete(
                synchronize_session=False)
        PaymentOrder.query.filter(PaymentOrder.invoice_id.in_(inv_ids)).delete(synchronize_session=False)
        if pay_ids:
            FeePayment.query.filter(FeePayment.id.in_(pay_ids)).delete(synchronize_session=False)
        FeeInvoice.query.filter(FeeInvoice.id.in_(inv_ids)).delete(synchronize_session=False)
    FeeStructure.query.filter(FeeStructure.id.in_(fs_ids)).delete(synchronize_session=False)
    db.session.commit()


def _invoice(client, env, student_key="a_id", **extra):
    body = {"student_id": str(env[student_key]), "fee_structure_id": str(env["fs_id"]), **extra}
    res = client.post("/api/v1/finance/invoices", headers=admin_headers(), json=body)
    assert res.status_code == 201, res.get_json()
    return res.get_json()["data"]


def _order(client, invoice_id, email=PARENT, key=None, **extra):
    body = {"invoice_id": invoice_id, "idempotency_key": key or f"idem-{uuid.uuid4().hex}", **extra}
    return client.post("/api/v1/payments/razorpay/order", headers=bearer_for(email), json=body)


def _sig(order_id, payment_id, secret=KEY_SECRET):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def _fresh(model, obj_id):
    db.session.expire_all()
    return db.session.get(model, obj_id)


def _live_order(client, monkeypatch, invoice_id, **extra):
    sent = {}

    def fake_create(amount_paise, receipt, notes):
        sent.update({"amount": amount_paise, "receipt": receipt, "notes": notes})
        return {"id": f"order_PT{uuid.uuid4().hex[:12]}", "amount": amount_paise, "currency": "INR"}

    monkeypatch.setattr(ps, "rzp_create_order", fake_create)
    res = _order(client, invoice_id, **extra)
    assert res.status_code == 201, res.get_json()
    return res.get_json()["data"], sent


def _captured(order, pid, status="captured", amount=None):
    return {"id": pid, "order_id": order["gateway_order_id"], "status": status, "currency": "INR",
            "amount": order["amount_paise"] if amount is None else amount, "method": "upi"}


def _webhook(client, body, event_id=None, secret=WEBHOOK_SECRET):
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "X-Razorpay-Signature": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
               "X-Razorpay-Event-Id": event_id or f"evt_paytest_{uuid.uuid4().hex[:12]}"}
    return client.post("/api/v1/payments/razorpay/webhook", data=raw, headers=headers)


def _pdf_text(blob: bytes) -> bytes:
    out = blob
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", blob, re.S):
        try:
            out += zlib.decompress(m.group(1))
        except zlib.error:
            pass
    return out


def _offline_pay(client, invoice_id, amount, method="CASH"):
    res = client.post(f"/api/v1/finance/invoices/{invoice_id}/pay", headers=admin_headers(),
                      json={"amount": amount, "payment_method": method, "transaction_reference": "CASH-1"})
    assert res.status_code == 201, res.get_json()
    return res.get_json()["data"]


# ---------------------------------------------------------------- config & startup

def test_payment_config_modes(client, mode):
    url = "/api/v1/payments/config"
    h = bearer_for(PARENT)
    mode("auto", key_id="", key_secret="")
    data = client.get(url, headers=h).get_json()["data"]
    assert data["mode"] == "disabled" and data["key_id"] is None and data["currency"] == "INR"
    mode("mock")
    data = client.get(url, headers=h).get_json()["data"]
    assert data["mode"] == "mock" and data["is_test"] is True and data["key_id"] is None
    mode("auto")
    data = client.get(url, headers=h).get_json()["data"]
    assert data["mode"] == "live" and data["is_test"] is True and data["key_id"] == KEY_ID
    assert KEY_SECRET not in json.dumps(data)
    assert client.get(url).status_code == 401


def test_live_mode_without_keys_fails_at_startup():
    with pytest.raises(RuntimeError, match="PAYMENT_MODE=live"):
        create_app("testing", config_overrides={"DATABASE_ENABLED": False, "SQLALCHEMY_DATABASE_URI": None,
                                                "PAYMENT_MODE": "live", "RAZORPAY_KEY_ID": "",
                                                "RAZORPAY_KEY_SECRET": ""})
    with pytest.raises(RuntimeError, match="PAYMENT_MODE"):
        create_app("testing", config_overrides={"DATABASE_ENABLED": False, "SQLALCHEMY_DATABASE_URI": None,
                                                "PAYMENT_MODE": "bogus"})


# ---------------------------------------------------------------- orders

def test_order_ownership_bounds_idempotency_and_disabled(client, env, mode):
    inv_a = _invoice(client, env)
    inv_b = _invoice(client, env, "b_id")
    mode("disabled")
    res = _order(client, inv_a["id"])
    assert res.status_code == 409 and res.get_json()["error"]["code"] == "PAYMENTS_DISABLED"

    mode("mock")
    # Parent of the ward, and the student themself, may pay.
    ok = _order(client, inv_a["id"], key="idem-parent-0001")
    assert ok.status_code == 201, ok.get_json()
    data = ok.get_json()["data"]
    assert data["mode"] == "mock" and data["is_test"] is True
    assert data["gateway_order_id"].startswith("order_mock_")
    assert data["amount_paise"] == 900000 and data["currency"] == "INR"
    assert data["invoice_number"] == inv_a["invoice_number"] and data["student_name"]
    assert data["prefill"]["email"] == PARENT
    assert _order(client, inv_a["id"], email=STUDENT_A).status_code == 201

    # Same key, same user → the same open order (no duplicate on double-click).
    again = _order(client, inv_a["id"], key="idem-parent-0001")
    assert again.status_code == 200 and again.get_json()["data"]["order_id"] == data["order_id"]
    assert PaymentOrder.query.filter_by(invoice_id=uuid.UUID(inv_a["id"])).count() == 2
    # Same key for another invoice is rejected.
    assert _order(client, inv_b["id"], key="idem-parent-0001").status_code == 409

    # Not the caller's student → 404 (no existence leak). Teachers cannot start checkouts.
    assert _order(client, inv_b["id"], email=STUDENT_A).status_code == 404
    assert _order(client, inv_a["id"], email=OTHER_PARENT).status_code == 404
    assert _order(client, inv_a["id"], email=TEACHER).status_code == 404
    assert _order(client, str(uuid.uuid4())).status_code == 404

    # Amount bounds and validation.
    for bad in (0.5, 0, -10, "abc", True, 9000.01):
        assert _order(client, inv_a["id"], amount=bad).status_code == 400, bad
    assert _order(client, inv_a["id"], key="short").status_code == 400
    assert client.post("/api/v1/payments/razorpay/order", headers=bearer_for(PARENT),
                       json={"invoice_id": "nope", "idempotency_key": "idem-xxxxxxxx"}).status_code == 400
    partial = _order(client, inv_a["id"], amount=1500)
    assert partial.status_code == 201 and partial.get_json()["data"]["amount_paise"] == 150000


def test_live_order_creation_binds_notes(client, env, mode, monkeypatch):
    mode("live")
    inv = _invoice(client, env)
    data, sent = _live_order(client, monkeypatch, inv["id"], amount=2500)
    assert data["mode"] == "live" and data["key_id"] == KEY_ID and data["is_test"] is True
    assert sent["amount"] == 250000
    assert sent["notes"] == {"invoice_id": inv["id"], "institution_id": str(env["inst_id"]),
                             "order_id": data["order_id"]}

    def gateway_down(*a, **k):
        raise ps.GatewayError("down", code="RAZORPAY_UNREACHABLE")
    monkeypatch.setattr(ps, "rzp_create_order", gateway_down)
    res = _order(client, inv["id"])
    assert res.status_code == 502 and "secret" not in json.dumps(res.get_json()).lower()


# ---------------------------------------------------------------- live verify

def test_live_verify_success_bad_signature_and_replay(client, env, mode, monkeypatch):
    mode("live")
    inv = _invoice(client, env)
    order, _ = _live_order(client, monkeypatch, inv["id"], amount=3000)
    pid = f"pay_PT{uuid.uuid4().hex[:12]}"
    fetched = []
    monkeypatch.setattr(ps, "rzp_fetch_payment", lambda p: fetched.append(p) or _captured(order, p))
    url = "/api/v1/payments/razorpay/verify"
    body = {"order_id": order["order_id"], "razorpay_order_id": order["gateway_order_id"],
            "razorpay_payment_id": pid, "razorpay_signature": _sig(order["gateway_order_id"], pid)}
    h = bearer_for(PARENT)

    bad = client.post(url, headers=h, json={**body, "razorpay_signature": "0" * 64})
    assert bad.status_code == 400 and bad.get_json()["success"] is False
    assert bad.get_json()["error"]["code"] == "SIGNATURE_MISMATCH"
    o = _fresh(PaymentOrder, uuid.UUID(order["order_id"]))
    assert o.status == "CREATED" and o.gateway_payment_id is None and not fetched

    # The signature binds the gateway order: a signature for another order fails too.
    other = client.post(url, headers=h, json={**body, "razorpay_order_id": "order_SOMETHINGELSE"})
    assert other.status_code == 400

    first = client.post(url, headers=h, json=body)
    assert first.status_code == 200, first.get_json()
    data = first.get_json()["data"]
    assert data["status"] == "SUCCEEDED" and RCT_RE.match(data["receipt_number"])
    assert data["invoice"]["balance_amount"] == 6000.0 and data["invoice"]["paid_amount"] == 3000.0

    replay = client.post(url, headers=h, json=body)
    assert replay.status_code == 200
    assert replay.get_json()["data"]["receipt_id"] == data["receipt_id"]
    assert FeePayment.query.filter_by(invoice_id=uuid.UUID(inv["id"])).count() == 1
    payment = FeePayment.query.filter_by(invoice_id=uuid.UUID(inv["id"])).one()
    assert payment.is_test is True and payment.order_id == uuid.UUID(order["order_id"])
    assert FeeReceipt.query.filter_by(payment_id=payment.id).count() == 1

    # Another family cannot verify or read this order.
    assert client.post(url, headers=bearer_for(OTHER_PARENT), json=body).status_code == 404
    assert client.get(f"/api/v1/payments/orders/{order['order_id']}",
                      headers=bearer_for(OTHER_PARENT)).status_code == 404
    got = client.get(f"/api/v1/payments/orders/{order['order_id']}", headers=h).get_json()["data"]
    assert got["status"] == "SUCCEEDED" and got["receipt_number"] == data["receipt_number"]


def test_live_verify_authorized_is_pending_then_captured(client, env, mode, monkeypatch):
    mode("live")
    inv = _invoice(client, env)
    order, _ = _live_order(client, monkeypatch, inv["id"], amount=1000)
    pid = f"pay_PA{uuid.uuid4().hex[:12]}"
    status = {"v": "authorized"}
    monkeypatch.setattr(ps, "rzp_fetch_payment", lambda p: _captured(order, p, status=status["v"]))
    body = {"order_id": order["order_id"], "razorpay_order_id": order["gateway_order_id"],
            "razorpay_payment_id": pid, "razorpay_signature": _sig(order["gateway_order_id"], pid)}
    res = client.post("/api/v1/payments/razorpay/verify", headers=bearer_for(PARENT), json=body)
    assert res.status_code == 202 and res.get_json()["success"] is True
    assert res.get_json()["data"]["status"] == "PENDING" and res.get_json()["data"]["receipt_id"] is None
    assert FeePayment.query.filter_by(invoice_id=uuid.UUID(inv["id"])).count() == 0
    assert FeeReceipt.query.filter_by(invoice_id=uuid.UUID(inv["id"])).count() == 0

    # A wrong amount from the gateway is never applied.
    status["v"] = "captured"
    monkeypatch.setattr(ps, "rzp_fetch_payment", lambda p: _captured(order, p, amount=1))
    res = client.post("/api/v1/payments/razorpay/verify", headers=bearer_for(PARENT), json=body)
    assert res.get_json()["data"]["status"] == "NEEDS_REVIEW"
    assert FeeReceipt.query.filter_by(invoice_id=uuid.UUID(inv["id"])).count() == 0


# ---------------------------------------------------------------- webhooks

def test_webhook_signature_capture_dedupe_failure(client, env, mode, monkeypatch):
    mode("live")
    inv = _invoice(client, env)
    order, _ = _live_order(client, monkeypatch, inv["id"], amount=2000)
    pid = f"pay_PW{uuid.uuid4().hex[:12]}"
    entity = _captured(order, pid)
    body = {"event": "payment.captured", "payload": {"payment": {"entity": entity}}}

    assert _webhook(client, body, secret="wrong-secret").status_code == 400
    raw = json.dumps(body).encode()
    unsigned = client.post("/api/v1/payments/razorpay/webhook", data=raw,
                           headers={"Content-Type": "application/json"})
    assert unsigned.status_code == 400
    assert _fresh(PaymentOrder, uuid.UUID(order["order_id"])).status == "CREATED"

    event_id = f"evt_paytest_{uuid.uuid4().hex[:10]}"
    res = _webhook(client, body, event_id=event_id)  # no JWT needed
    assert res.status_code == 200, res.get_json()
    o = _fresh(PaymentOrder, uuid.UUID(order["order_id"]))
    assert o.status == "SUCCEEDED" and o.gateway_payment_id == pid
    assert FeeReceipt.query.filter_by(payment_id=o.payment_id).count() == 1

    dup = _webhook(client, body, event_id=event_id)
    assert dup.status_code == 200 and dup.get_json()["data"]["duplicate"] is True
    # order.paid for the same payment (new event id) is also a no-op.
    assert _webhook(client, {"event": "order.paid", "payload": {"payment": {"entity": entity}}}).status_code == 200
    assert FeePayment.query.filter_by(invoice_id=uuid.UUID(inv["id"])).count() == 1

    # payment.failed marks a fresh order FAILED; a failure never undoes a success.
    order2, _ = _live_order(client, monkeypatch, inv["id"], amount=500)
    failed = {**_captured(order2, f"pay_PF{uuid.uuid4().hex[:12]}", status="failed"),
              "error_description": "Bank declined"}
    assert _webhook(client, {"event": "payment.failed", "payload": {"payment": {"entity": failed}}}).status_code == 200
    o2 = _fresh(PaymentOrder, uuid.UUID(order2["order_id"]))
    assert o2.status == "FAILED" and o2.failure_reason == "Bank declined"
    late_fail = {**entity, "status": "failed"}
    _webhook(client, {"event": "payment.failed", "payload": {"payment": {"entity": late_fail}}})
    assert _fresh(PaymentOrder, uuid.UUID(order["order_id"])).status == "SUCCEEDED"

    # Unknown orders are acknowledged and ignored.
    stray = {**entity, "id": "pay_STRAY1", "order_id": "order_UNKNOWN1"}
    assert _webhook(client, {"event": "payment.captured", "payload": {"payment": {"entity": stray}}}).status_code == 200


def test_live_refund_pending_until_refund_processed_webhook(client, env, mode, monkeypatch):
    mode("live")
    inv = _invoice(client, env)
    order, _ = _live_order(client, monkeypatch, inv["id"], amount=4000)
    pid = f"pay_PR{uuid.uuid4().hex[:12]}"
    _webhook(client, {"event": "payment.captured", "payload": {"payment": {"entity": _captured(order, pid)}}})
    payment_id = _fresh(PaymentOrder, uuid.UUID(order["order_id"])).payment_id
    rfnd = f"rfnd_PT{uuid.uuid4().hex[:10]}"
    calls = []
    monkeypatch.setattr(ps, "rzp_create_refund",
                        lambda p, paise, notes: calls.append((p, paise)) or {"id": rfnd, "status": "pending"})
    res = client.post(f"/api/v1/finance/payments/{payment_id}/refund", headers=admin_headers(),
                      json={"amount": 1500, "reason": "Fee revision"})
    assert res.status_code == 202, res.get_json()
    assert calls == [(pid, 150000)]
    refund = FeeRefund.query.filter_by(gateway_refund_id=rfnd).one()
    assert refund.status == "PENDING"
    assert _fresh(FeeInvoice, uuid.UUID(inv["id"])).balance_amount == Decimal("5000.00")
    # The pending amount is reserved: a refund beyond the rest is refused.
    over = client.post(f"/api/v1/finance/payments/{payment_id}/refund", headers=admin_headers(),
                       json={"amount": 2500.01, "reason": "too much"})
    assert over.status_code == 400

    ref_entity = {"id": rfnd, "payment_id": pid, "amount": 150000, "status": "processed"}
    assert _webhook(client, {"event": "refund.processed", "payload": {"refund": {"entity": ref_entity}}}).status_code == 200
    assert _fresh(FeeRefund, refund.id).status == "PROCESSED"
    inv_row = _fresh(FeeInvoice, uuid.UUID(inv["id"]))
    assert inv_row.refunded_amount == Decimal("1500.00") and inv_row.balance_amount == Decimal("6500.00")
    p = _fresh(FeePayment, payment_id)
    assert p.status == "PARTIALLY_REFUNDED" and p.refunded_amount == Decimal("1500.00")


# ---------------------------------------------------------------- mock gateway

def test_mock_flow_success_failure_cancel(client, env, mode):
    mode("mock")
    inv = _invoice(client, env)
    h = bearer_for(PARENT)
    url = "/api/v1/payments/mock/orders/{}/complete"

    o1 = _order(client, inv["id"], amount=1000).get_json()["data"]
    res = client.post(url.format(o1["order_id"]), headers=h, json={"outcome": "success"})
    assert res.status_code == 200, res.get_json()
    data = res.get_json()["data"]
    assert data["status"] == "SUCCEEDED" and data["is_test"] is True and RCT_RE.match(data["receipt_number"])
    assert data["gateway_payment_id"].startswith("pay_mock_")
    receipt = client.get(f"/api/v1/finance/receipts/{data['receipt_id']}", headers=h).get_json()["data"]
    assert receipt["is_test"] is True and receipt["snapshot"]["is_test"] is True
    pdf = client.get(f"/api/v1/finance/receipts/{data['receipt_id']}/pdf", headers=h)
    assert b"TEST PAYMENT - NOT A REAL TRANSACTION" in _pdf_text(pdf.data)
    # Completing again is idempotent.
    again = client.post(url.format(o1["order_id"]), headers=h, json={"outcome": "success"})
    assert again.get_json()["data"]["receipt_id"] == data["receipt_id"]
    assert FeePayment.query.filter_by(invoice_id=uuid.UUID(inv["id"])).count() == 1

    o2 = _order(client, inv["id"], amount=500).get_json()["data"]
    failed = client.post(url.format(o2["order_id"]), headers=h, json={"outcome": "failure"}).get_json()["data"]
    assert failed["status"] == "FAILED" and failed["failure_reason"] and failed["receipt_id"] is None

    o3 = _order(client, inv["id"], amount=500).get_json()["data"]
    cancelled = client.post(url.format(o3["order_id"]), headers=h, json={"outcome": "cancel"}).get_json()["data"]
    assert cancelled["status"] == "CANCELLED"
    # Cancel only from CREATED; fail endpoint reports its own reason.
    o4 = _order(client, inv["id"], amount=500).get_json()["data"]
    r = client.post(f"/api/v1/payments/orders/{o4['order_id']}/fail", headers=h, json={"reason": "UPI timeout"})
    assert r.get_json()["data"]["status"] == "FAILED" and r.get_json()["data"]["failure_reason"] == "UPI timeout"
    assert client.post(f"/api/v1/payments/orders/{o4['order_id']}/cancel", headers=h).status_code == 409
    assert client.post(url.format(o4["order_id"]), headers=h, json={"outcome": "maybe"}).status_code == 400

    assert _fresh(FeeInvoice, uuid.UUID(inv["id"])).paid_amount == Decimal("1000.00")
    # Mock completion does not exist outside mock mode.
    mode("live")
    assert client.post(url.format(o2["order_id"]), headers=h, json={"outcome": "success"}).status_code == 404


def test_paid_meanwhile_needs_review_without_receipt(client, env, mode):
    mode("mock")
    inv = _invoice(client, env)
    order = _order(client, inv["id"], amount=9000).get_json()["data"]
    _offline_pay(client, inv["id"], 9000)
    res = client.post(f"/api/v1/payments/mock/orders/{order['order_id']}/complete", headers=bearer_for(PARENT),
                      json={"outcome": "success"})
    data = res.get_json()["data"]
    assert data["status"] == "NEEDS_REVIEW" and data["receipt_id"] is None
    assert FeePayment.query.filter_by(invoice_id=uuid.UUID(inv["id"])).count() == 1
    assert AuditLog.query.filter_by(entity_id=uuid.UUID(order["order_id"]), action="PAYMENT_NEEDS_REVIEW").count() == 1


# ---------------------------------------------------------------- installments & adjustments

def test_installments_allocation_and_overdue(client, env):
    past = (date.today() - timedelta(days=45)).isoformat()
    inv = _invoice(client, env, installment_count=3, due_date=past)
    insts = inv["installments"]
    assert [i["amount"] for i in insts] == [3000.0, 3000.0, 3000.0]
    assert insts[0]["status"] == "OVERDUE" and inv["is_overdue"] is True
    assert inv["next_payable_amount"] == 3000.0 + (3000.0 if insts[1]["status"] == "OVERDUE" else 0)

    _offline_pay(client, inv["id"], 4000)
    detail = client.get(f"/api/v1/finance/invoices/{inv['id']}", headers=admin_headers()).get_json()["data"]
    i1, i2, i3 = detail["installments"]
    assert i1["status"] == "PAID" and i1["paid_amount"] == 3000.0
    assert i2["paid_amount"] == 1000.0 and i2["status"] in ("PARTIALLY_PAID", "OVERDUE")
    assert i3["paid_amount"] == 0.0
    assert detail["balance_amount"] == 5000.0 and detail["status"] == "PARTIALLY_PAID"

    fees = client.get(f"/api/v1/finance/my-fees?student_id={env['a_id']}", headers=bearer_for(PARENT))
    assert fees.status_code == 200
    entry = fees.get_json()["data"]["students"][0]
    assert entry["student"]["admission_number"] and entry["student"]["class_name"]
    portal_inv = next(i for i in entry["invoices"] if i["id"] == inv["id"])
    assert portal_inv["is_overdue"] is True
    assert entry["summary"]["overdue"] >= 2000.0
    assert any(p["receipt_number"] for p in entry["payments"])

    bad = client.post("/api/v1/finance/invoices", headers=admin_headers(), json={
        "student_id": str(env["b_id"]), "fee_structure_id": str(env["fs_id"]),
        "installments": [{"label": "Q1", "amount": 5000, "due_date": "2026-12-01"},
                         {"label": "Q2", "amount": 3000, "due_date": "2027-01-01"}]})
    assert bad.status_code == 400 and bad.get_json()["error"]["code"] == "INSTALLMENT_MISMATCH"
    # Duplicate invoice for the same structure is refused.
    dup = client.post("/api/v1/finance/invoices", headers=admin_headers(),
                      json={"student_id": str(env["a_id"]), "fee_structure_id": str(env["fs_id"])})
    assert dup.status_code == 409


def test_adjustments_change_totals_and_installments(client, env):
    inv = _invoice(client, env, installment_count=2,
                   adjustments=[{"kind": "SCHOLARSHIP", "label": "Merit", "amount": 1000}])
    assert inv["total_amount"] == 8000.0 and inv["discount_amount"] == 1000.0
    url = f"/api/v1/finance/invoices/{inv['id']}/adjustments"
    h = admin_headers()
    r = client.post(url, headers=h, json={"kind": "DISCOUNT", "label": "Sibling", "amount": 500})
    assert r.status_code == 200, r.get_json()
    r = client.post(url, headers=h, json={"kind": "LATE_FINE", "label": "Late fee", "amount": 200})
    data = r.get_json()["data"]
    assert data["discount_amount"] == 1500.0 and data["fine_amount"] == 200.0
    assert data["total_amount"] == 7700.0 and data["balance_amount"] == 7700.0
    assert round(sum(i["amount"] for i in data["installments"]), 2) == 7700.0

    _offline_pay(client, inv["id"], 5000)
    # A discount that would push the total below what was paid is refused.
    assert client.post(url, headers=h, json={"kind": "WAIVER", "label": "x", "amount": 3000}).status_code == 400
    for bad in ({"kind": "BONUS", "label": "x", "amount": 1}, {"kind": "DISCOUNT", "label": "x", "amount": -1},
                {"kind": "DISCOUNT", "label": 123, "amount": 1}, [1]):
        assert client.post(url, headers=h, json=bad).status_code == 400

    fine_id = next(a["id"] for a in data["adjustments"] if a["kind"] == "LATE_FINE")
    r = client.delete(f"{url}/{fine_id}", headers=h)
    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    assert data["total_amount"] == 7500.0 and data["balance_amount"] == 2500.0
    assert round(sum(i["amount"] for i in data["installments"]), 2) == 7500.0
    assert round(sum(i["paid_amount"] for i in data["installments"]), 2) == 5000.0
    assert client.delete(f"{url}/{uuid.uuid4()}", headers=h).status_code == 404
    # Admin only.
    assert client.post(url, headers=bearer_for(PARENT), json={"kind": "DISCOUNT", "label": "x", "amount": 1}).status_code == 403
    assert AuditLog.query.filter_by(entity_id=uuid.UUID(inv["id"]), action="INVOICE_ADJUSTED").count() >= 3


def test_generate_invoices_skips_existing(client, env):
    _invoice(client, env, "b_id")
    url = f"/api/v1/finance/fee-structures/{env['fs_id']}/generate-invoices"
    res = client.post(url, headers=admin_headers(), json={"section_id": str(env["section_id"]), "installment_count": 2})
    assert res.status_code == 200, res.get_json()
    first = res.get_json()["data"]
    assert first["created"] >= 1 and first["skipped"] >= 1
    again = client.post(url, headers=admin_headers(), json={"section_id": str(env["section_id"])}).get_json()["data"]
    assert again["created"] == 0 and again["skipped"] >= first["created"] + 1
    assert client.post(url, headers=bearer_for(TEACHER), json={}).status_code == 403


# ---------------------------------------------------------------- refunds

def test_offline_refunds_partial_then_full(client, env):
    inv = _invoice(client, env, installment_count=3)
    paid = _offline_pay(client, inv["id"], 3000)
    assert RCT_RE.match(paid["receipt_number"])
    url = f"/api/v1/finance/payments/{paid['payment_id']}/refund"
    h = admin_headers()
    assert client.post(url, headers=bearer_for(PARENT), json={"amount": 1, "reason": "x"}).status_code == 403
    for bad in ({"amount": 0, "reason": "x"}, {"amount": "abc", "reason": "x"}, {"amount": 10},
                {"amount": 10, "reason": ""}):
        assert client.post(url, headers=h, json=bad).status_code == 400

    r1 = client.post(url, headers=h, json={"amount": 1000, "reason": "Overcharged"})
    assert r1.status_code == 201, r1.get_json()
    d1 = r1.get_json()["data"]
    assert d1["refund"]["status"] == "PROCESSED" and d1["payment"]["status"] == "PARTIALLY_REFUNDED"
    assert d1["payment"]["refundable_amount"] == 2000.0
    assert d1["invoice"]["balance_amount"] == 7000.0 and d1["invoice"]["refunded_amount"] == 1000.0

    over = client.post(url, headers=h, json={"amount": 2000.01, "reason": "too much"})
    assert over.status_code == 400 and over.get_json()["error"]["code"] == "OVER_REFUND"

    r2 = client.post(url, headers=h, json={"amount": 2000, "reason": "Withdrawn"})
    d2 = r2.get_json()["data"]
    assert d2["payment"]["status"] == "REFUNDED" and d2["invoice"]["balance_amount"] == 9000.0
    assert d2["invoice"]["status"] == "UNPAID"
    assert client.post(url, headers=h, json={"amount": 1, "reason": "again"}).status_code == 400

    db.session.expire_all()
    insts = FeeInstallment.query.filter_by(invoice_id=uuid.UUID(inv["id"])).all()
    assert sum(i.paid_amount for i in insts) == Decimal("0.00")
    debits = FinancialTransaction.query.filter_by(payment_id=uuid.UUID(paid["payment_id"]),
                                                  credit_or_debit="DEBIT").all()
    assert sorted(t.amount for t in debits) == [Decimal("1000.00"), Decimal("2000.00")]
    assert client.post(f"/api/v1/finance/payments/{uuid.uuid4()}/refund", headers=h,
                       json={"amount": 1, "reason": "x"}).status_code == 404

    # The balance re-opened: the invoice can be paid again.
    _offline_pay(client, inv["id"], 9000)
    assert _fresh(FeeInvoice, uuid.UUID(inv["id"])).status == "PAID"

    receipt = client.get(f"/api/v1/finance/receipts/{paid['receipt_id']}", headers=h).get_json()["data"]
    assert receipt["refund_status"] == "FULL" and receipt["snapshot"]["amount_paid"] == 3000.0
    pdf = client.get(f"/api/v1/finance/receipts/{paid['receipt_id']}/pdf", headers=h)
    assert b"REFUNDED" in _pdf_text(pdf.data)

    listing = client.get(f"/api/v1/finance/payments?student_id={env['a_id']}&status=REFUNDED", headers=h)
    assert listing.status_code == 200
    page = listing.get_json()["data"]
    assert {"items", "total", "page", "pages", "limit"} <= set(page)
    assert any(i["id"] == paid["payment_id"] and i["kind"] == "payment" for i in page["items"])


# ---------------------------------------------------------------- receipts

def test_receipt_pdf_ownership_and_numbering(client, env):
    inv = _invoice(client, env)
    p1 = _offline_pay(client, inv["id"], 1000)
    p2 = _offline_pay(client, inv["id"], 1000)
    n1, n2 = p1["receipt_number"], p2["receipt_number"]
    assert n1[:9] == n2[:9] and int(n2[-6:]) == int(n1[-6:]) + 1

    rid = p1["receipt_id"]
    for email in (PARENT, STUDENT_A, PRINCIPAL, TEACHER):
        res = client.get(f"/api/v1/finance/receipts/{rid}/pdf", headers=bearer_for(email))
        assert res.status_code == 200, email
    res = client.get(f"/api/v1/finance/receipts/{rid}/pdf", headers=bearer_for(PARENT))
    assert res.mimetype == "application/pdf" and res.data.startswith(b"%PDF")
    assert res.headers["Content-Disposition"] == f"inline; filename={n1}.pdf"
    text = _pdf_text(res.data)
    assert n1.encode() in text and b"Rs. 1,000.00" in text and b"TEST PAYMENT" not in text

    for email in (OTHER_PARENT, STUDENT_B):
        h = bearer_for(email)
        assert client.get(f"/api/v1/finance/receipts/{rid}", headers=h).status_code == 404
        assert client.get(f"/api/v1/finance/receipts/{rid}/pdf", headers=h).status_code == 404
        ids = [r["id"] for r in client.get("/api/v1/finance/receipts", headers=h).get_json()["data"]["items"]]
        assert rid not in ids
        assert client.get(f"/api/v1/finance/my-fees?student_id={env['a_id']}", headers=h).status_code == 404
    mine = client.get("/api/v1/finance/receipts", headers=bearer_for(PARENT)).get_json()["data"]["items"]
    row = next(r for r in mine if r["id"] == rid)
    assert row["receipt_number"] == n1 and row["amount"] == 1000.0 and row["invoice_number"] == inv["invoice_number"]
    assert client.get(f"/api/v1/finance/receipts/{uuid.uuid4()}", headers=bearer_for(PARENT)).status_code == 404

    # Portal visibility rules.
    other = client.get("/api/v1/finance/my-fees", headers=bearer_for(OTHER_PARENT)).get_json()["data"]["students"]
    assert [s["student"]["id"] for s in other] == [str(env["b_id"])]
    me = client.get("/api/v1/finance/my-fees", headers=bearer_for(STUDENT_A)).get_json()["data"]["students"]
    assert [s["student"]["id"] for s in me] == [str(env["a_id"])]
    assert client.get("/api/v1/finance/my-fees", headers=admin_headers()).status_code == 400
    assert client.get(f"/api/v1/finance/my-fees?student_id={env['a_id']}", headers=admin_headers()).status_code == 200


def test_receipt_snapshot_survives_fee_structure_change(client, env):
    inv = _invoice(client, env)
    paid = _offline_pay(client, inv["id"], 2000)
    h = admin_headers()
    before = client.get(f"/api/v1/finance/receipts/{paid['receipt_id']}", headers=h).get_json()["data"]["snapshot"]
    item = FeeStructureItem.query.filter_by(fee_structure_id=env["fs_id"], fee_head="Tuition Fee").one()
    item.fee_head, item.amount = "Tuition (revised)", Decimal("9999.00")
    fs = db.session.get(FeeStructure, env["fs_id"])
    fs.name = fs.name + " v2"
    db.session.commit()
    _offline_pay(client, inv["id"], 500)
    after = client.get(f"/api/v1/finance/receipts/{paid['receipt_id']}", headers=h).get_json()["data"]["snapshot"]
    assert after == before
    assert {"fee_head": "Tuition Fee", "amount": 6000.0} in after["items"]
    assert after["balance_after"] == 7000.0 and after["amount_paid"] == 2000.0
    assert after["student"]["name"] and after["school"]["name"]


def test_admin_transaction_history_includes_orders(client, env, mode):
    mode("mock")
    inv = _invoice(client, env)
    order = _order(client, inv["id"], amount=700).get_json()["data"]
    client.post(f"/api/v1/payments/orders/{order['order_id']}/cancel", headers=bearer_for(PARENT))
    _offline_pay(client, inv["id"], 1200)
    h = admin_headers()
    page = client.get(f"/api/v1/finance/payments?student_id={env['a_id']}", headers=h).get_json()["data"]
    kinds = {(i["kind"], i["status"]) for i in page["items"]}
    assert ("order", "CANCELLED") in kinds and ("payment", "SUCCESS") in kinds
    pay_row = next(i for i in page["items"] if i["kind"] == "payment")
    assert pay_row["student_name"] and pay_row["receipt_number"] and pay_row["refundable_amount"] == 1200.0
    only_cancelled = client.get(f"/api/v1/finance/payments?student_id={env['a_id']}&status=CANCELLED",
                                headers=h).get_json()["data"]["items"]
    assert only_cancelled and all(i["kind"] == "order" for i in only_cancelled)
    assert client.get("/api/v1/finance/payments?status=BOGUS", headers=h).status_code == 400
    assert client.get("/api/v1/finance/payments?from=31-12-2026", headers=h).status_code == 400
    assert client.get("/api/v1/finance/payments", headers=bearer_for(PARENT)).status_code == 403
