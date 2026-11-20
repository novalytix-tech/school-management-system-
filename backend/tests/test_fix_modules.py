"""
Regression tests for the finance / payments / library / admissions /
question-paper / communications / examinations / transport / system / sockets /
cache fixes. Opt-in (needs TEST_DATABASE_URL). Every test restores the rows it
creates. No real Razorpay / SMS / SMTP traffic: network calls are mocked and
gateway keys are overridden with test values.
"""
import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from flask import jsonify
from flask_jwt_extended import create_access_token

from app.extensions import db, socketio
from app.models import (
    Institution, User, Role, UserRole, Student, ClassLevel, AcademicYear, Subject,
    FeeStructure, FeeInvoice, FeePayment, FinancialTransaction,
    Book, BookCopy, BookLoan,
    AdmissionApplication, AdmissionInquiry, QuestionPaper, QuestionPaperVersion,
    Message, TokenBlocklist,
)
from app.utils.security import hash_password
from tests._auth import admin_headers, bearer_for

PRINCIPAL = "principal@stxaviers.edu.in"
TEACHER = "ananya.sharma@stxaviers.edu.in"
TEACHER2 = "priya.nair@stxaviers.edu.in"
PARENT = "vikram.patel@gmail.com"
B_ADMIN = "admin.fixmodules@campusb.test"
B_CODE = "FIXMOD-B"


# ---------------------------------------------------------------- fixtures

def _inst_a():
    return Institution.query.filter_by(code="STXAV-DELHI").first()


@pytest.fixture
def inst_b(app):
    """A second institution with its own school admin (removed afterwards)."""
    inst = Institution(name="Fix Modules Campus B", code=B_CODE, email="b@campusb.test",
                       board_affiliation="CBSE", currency="INR", status="ACTIVE")
    db.session.add(inst)
    db.session.flush()
    user = User(institution_id=inst.id, username="fixmod_b_admin", email=B_ADMIN,
                password_hash=hash_password("SchoolERP@Demo2026!"),
                first_name="Bee", last_name="Admin", is_active=True)
    db.session.add(user)
    db.session.flush()
    role = Role.query.filter_by(name="school_admin").first()
    db.session.add(UserRole(user_id=user.id, role_id=role.id))
    db.session.commit()
    inst_id, user_id = inst.id, user.id
    yield inst
    db.session.rollback()
    for b in Book.query.filter_by(institution_id=inst_id).all():
        db.session.delete(b)
    AdmissionInquiry.query.filter_by(institution_id=inst_id).delete()
    ClassLevel.query.filter_by(institution_id=inst_id).delete()
    UserRole.query.filter_by(user_id=user_id).delete()
    TokenBlocklist.query.filter_by(user_id=user_id).delete()
    User.query.filter_by(id=user_id).delete()
    Institution.query.filter_by(id=inst_id).delete()
    db.session.commit()


@pytest.fixture
def invoice(app):
    """A fresh unpaid 5000.00 invoice in St. Xavier's (removed afterwards)."""
    inst = _inst_a()
    student = Student.query.filter_by(institution_id=inst.id).first()
    fs = FeeStructure.query.filter_by(institution_id=inst.id).first()
    created_fs = None
    if not fs:
        fs = FeeStructure(
            institution_id=inst.id,
            academic_year_id=AcademicYear.query.filter_by(institution_id=inst.id).first().id,
            class_level_id=ClassLevel.query.filter_by(institution_id=inst.id).first().id,
            name=f"FixMod FS {uuid.uuid4().hex[:6]}", due_date=date(2026, 12, 31))
        db.session.add(fs)
        db.session.flush()
        created_fs = fs.id
    inv = FeeInvoice(
        institution_id=inst.id, invoice_number=f"INV-FIXMOD-{uuid.uuid4().hex[:8]}",
        student_id=student.id, fee_structure_id=fs.id,
        subtotal=Decimal("5000.00"), discount_amount=Decimal("0"), adjustment_amount=Decimal("0"),
        total_amount=Decimal("5000.00"), paid_amount=Decimal("0"), balance_amount=Decimal("5000.00"),
        status="UNPAID", due_date=date(2026, 12, 31))
    db.session.add(inv)
    db.session.commit()
    inv_id = inv.id
    yield inv
    db.session.rollback()
    pay_ids = [p.id for p in FeePayment.query.filter_by(invoice_id=inv_id).all()]
    if pay_ids:
        FinancialTransaction.query.filter(FinancialTransaction.payment_id.in_(pay_ids)).delete(
            synchronize_session=False)
        FeePayment.query.filter(FeePayment.id.in_(pay_ids)).delete(synchronize_session=False)
    FeeInvoice.query.filter_by(id=inv_id).delete()
    if created_fs:
        FeeStructure.query.filter_by(id=created_fs).delete()
    db.session.commit()


def _fresh(model, obj_id):
    db.session.expire_all()
    return db.session.get(model, obj_id)


# ---------------------------------------------------------------- finance

def test_invoice_lookup_and_payment_are_tenant_scoped(client, inst_b, invoice):
    b = bearer_for(B_ADMIN)
    assert client.get(f"/api/v1/finance/invoices/{invoice.id}", headers=b).status_code == 404
    res = client.post(f"/api/v1/finance/invoices/{invoice.id}/pay", headers=b, json={"amount": 10})
    assert res.status_code == 404
    assert _fresh(FeeInvoice, invoice.id).paid_amount == Decimal("0.00")
    assert client.get(f"/api/v1/finance/invoices/{invoice.id}", headers=admin_headers()).status_code == 200


def test_payment_links_ledger_unique_numbers_and_rejects_overpay(client, invoice):
    h = admin_headers()
    url = f"/api/v1/finance/invoices/{invoice.id}/pay"
    r1 = client.post(url, headers=h, json={"amount": "1000.50", "payment_method": "upi"})
    r2 = client.post(url, headers=h, json={"amount": 999.5})
    assert r1.status_code == 201 and r2.status_code == 201, (r1.get_json(), r2.get_json())
    n1, n2 = r1.get_json()["data"]["payment_number"], r2.get_json()["data"]["payment_number"]
    assert n1 != n2

    for r in (r1, r2):
        pid = uuid.UUID(r.get_json()["data"]["payment_id"])
        tx = FinancialTransaction.query.filter_by(payment_id=pid).one()
        assert tx.amount > 0

    over = client.post(url, headers=h, json={"amount": 3000.01})
    assert over.status_code == 400
    assert client.post(url, headers=h, json={"amount": 3000}).status_code == 201
    inv = _fresh(FeeInvoice, invoice.id)
    assert inv.status == "PAID" and inv.balance_amount == Decimal("0.00")
    # A PAID invoice accepts nothing further.
    assert client.post(url, headers=h, json={"amount": 1}).status_code == 409


@pytest.mark.parametrize("body", [
    {"amount": "abc"}, {"amount": "NaN"}, {"amount": "Infinity"}, {"amount": "0.001"},
    {"amount": -5}, {"amount": True}, {"amount": [1]},
    {"amount": 10, "payment_method": 5},
])
def test_payment_amount_parsing_rejects_garbage(client, invoice, body):
    res = client.post(f"/api/v1/finance/invoices/{invoice.id}/pay", headers=admin_headers(), json=body)
    assert res.status_code == 400, res.get_json()


def test_payment_list_body_is_400(client, invoice):
    res = client.post(f"/api/v1/finance/invoices/{invoice.id}/pay", headers=admin_headers(), json=[1, 2])
    assert res.status_code == 400


def test_fee_structure_create_validation(client, inst_b):
    inst = _inst_a()
    year = AcademicYear.query.filter_by(institution_id=inst.id).first()
    cls = ClassLevel.query.filter_by(institution_id=inst.id).first()
    h = admin_headers()
    base = {"name": f"FixMod {uuid.uuid4().hex[:6]}", "academic_year_id": str(year.id),
            "class_level_id": str(cls.id)}
    url = "/api/v1/finance/fee-structures"
    assert client.post(url, headers=h, json={**base, "academic_year_id": "nope"}).status_code == 400
    assert client.post(url, headers=h, json={**base, "academic_year_id": str(uuid.uuid4())}).status_code == 404
    assert client.post(url, headers=h, json={**base, "due_date": "31/12/2026"}).status_code == 400
    assert client.post(url, headers=h, json={**base, "items": [{"amount": "lots"}]}).status_code == 400
    assert client.post(url, headers=h, json={**base, "items": [{"amount": -1}]}).status_code == 400
    # Another tenant cannot use St. Xavier's year/class ids.
    assert client.post(url, headers=bearer_for(B_ADMIN), json=base).status_code == 404
    assert FeeStructure.query.filter_by(name=base["name"]).count() == 0


def test_invoice_list_bad_student_id_is_400(client):
    assert client.get("/api/v1/finance/invoices?student_id=xyz", headers=admin_headers()).status_code == 400


# ---------------------------------------------------------------- payments (Razorpay, mocked)

class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def rzp_keys(app, monkeypatch):
    monkeypatch.setitem(app.config, "RAZORPAY_KEY_ID", "rzp_test_fixmodules")
    monkeypatch.setitem(app.config, "RAZORPAY_KEY_SECRET", "fixmodules-test-secret")
    return "fixmodules-test-secret"


def test_razorpay_order_validation(client, invoice, rzp_keys):
    h = admin_headers()
    url = "/api/v1/payments/razorpay/order"
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent.update(json.loads(req.data.decode()))
        return _FakeResp({"id": "order_FIXMOD1", "amount": sent["amount"], "currency": "INR"})

    with patch("app.api.v1.payments.urllib.request.urlopen", side_effect=fake_urlopen) as mocked:
        assert client.post(url, headers=h, json={"amount": 10000}).status_code == 400  # no invoice
        for bad in (100.9, "12.5", True, -100, 50):
            assert client.post(url, headers=h, json={"amount": bad, "invoice_id": str(invoice.id)}).status_code == 400
        assert client.post(url, headers=h, json={"amount": 500001, "invoice_id": str(invoice.id)}).status_code == 400
        assert client.post(url, headers=h, json={"amount": 10000, "currency": "USD",
                                                 "invoice_id": str(invoice.id)}).status_code == 400
        assert mocked.call_count == 0
        ok = client.post(url, headers=h, json={"amount": 10000, "invoice_id": str(invoice.id)})
    assert ok.status_code == 200, ok.get_json()
    assert sent["notes"]["invoice_id"] == str(invoice.id)
    assert sent["notes"]["institution_id"] == str(invoice.institution_id)


def test_razorpay_verify_records_once(client, invoice, rzp_keys):
    h = admin_headers()
    order_id, payment_id = "order_FIXMOD2", f"pay_FIXMOD{uuid.uuid4().hex[:8]}"
    sig = hmac.new(rzp_keys.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    order = {"id": order_id, "currency": "INR", "amount": 150000, "amount_paid": 150000, "status": "paid",
             "notes": {"invoice_id": str(invoice.id), "institution_id": str(invoice.institution_id)}}
    body = {"razorpay_order_id": order_id, "razorpay_payment_id": payment_id,
            "razorpay_signature": sig, "invoice_id": str(invoice.id)}
    url = "/api/v1/payments/razorpay/verify"

    captured = {"id": payment_id, "order_id": order_id, "status": "captured", "amount": 150000}
    with patch("app.api.v1.payments._fetch_order", return_value=order),             patch("app.api.v1.payments._fetch_payment", return_value=captured):
        bad = client.post(url, headers=h, json={**body, "razorpay_signature": "0" * 64})
        assert bad.status_code == 400
        first = client.post(url, headers=h, json=body)
        assert first.status_code == 201, first.get_json()
        replay = client.post(url, headers=h, json=body)
        assert replay.status_code == 200 and replay.get_json()["data"]["duplicate"] is True

    inv = _fresh(FeeInvoice, invoice.id)
    assert inv.paid_amount == Decimal("1500.00")
    assert FeePayment.query.filter_by(invoice_id=invoice.id).count() == 1

    # An order bound to another invoice cannot be used to pay this one.
    other = {**order, "notes": {"invoice_id": str(uuid.uuid4()), "institution_id": str(invoice.institution_id)}}
    pid2 = "pay_FIXMODX" + uuid.uuid4().hex[:6]
    sig2 = hmac.new(rzp_keys.encode(), f"{order_id}|{pid2}".encode(), hashlib.sha256).hexdigest()
    with patch("app.api.v1.payments._fetch_order", return_value=other),             patch("app.api.v1.payments._fetch_payment", return_value={**captured, "id": pid2}):
        res = client.post(url, headers=h, json={**body, "razorpay_payment_id": pid2, "razorpay_signature": sig2})
    assert res.status_code == 400


def test_razorpay_verify_uncaptured_and_unappliable_payments_are_flagged(client, invoice, rzp_keys):
    """Verified money is never dropped silently: retryable or flagged for reconciliation."""
    h = admin_headers()
    order_id = "order_FIXMOD3"
    order = {"id": order_id, "currency": "INR", "amount": 150000, "amount_paid": 0, "status": "attempted",
             "notes": {"invoice_id": str(invoice.id), "institution_id": str(invoice.institution_id)}}
    url = "/api/v1/payments/razorpay/verify"

    def _body(pid):
        sig = hmac.new(rzp_keys.encode(), f"{order_id}|{pid}".encode(), hashlib.sha256).hexdigest()
        return {"razorpay_order_id": order_id, "razorpay_payment_id": pid,
                "razorpay_signature": sig, "invoice_id": str(invoice.id)}

    # Authorized (manual capture): verified, not recorded, retryable.
    pid = "pay_FIXAUTH" + uuid.uuid4().hex[:6]
    with patch("app.api.v1.payments._fetch_order", return_value=order),             patch("app.api.v1.payments._fetch_payment",
                  return_value={"id": pid, "order_id": order_id, "status": "authorized", "amount": 150000}):
        res = client.post(url, headers=h, json=_body(pid))
    assert res.status_code == 409
    assert res.get_json()["data"] == {"verified": True, "recorded": False}

    # Captured but larger than the balance: flagged for reconciliation, not recorded.
    pid = "pay_FIXOVER" + uuid.uuid4().hex[:6]
    with patch("app.api.v1.payments._fetch_order", return_value=order),             patch("app.api.v1.payments._fetch_payment",
                  return_value={"id": pid, "order_id": order_id, "status": "captured", "amount": 99_999_999}):
        res = client.post(url, headers=h, json=_body(pid))
    assert res.status_code == 409
    body = res.get_json()
    assert body["error"]["code"] == "NEEDS_RECONCILIATION"
    assert body["data"] == {"verified": True, "recorded": False}
    assert FeePayment.query.filter_by(invoice_id=invoice.id).count() == 0


# ---------------------------------------------------------------- library

def test_library_same_isbn_other_tenant_and_copies_validation(client, inst_b):
    isbn = f"FIX-{uuid.uuid4().hex[:8]}"
    h = admin_headers()
    url = "/api/v1/library/books"
    try:
        assert client.post(url, headers=h, json={"title": "T", "author": "A", "isbn": isbn,
                                                 "copies_count": 2}).status_code == 201
        res_b = client.post(url, headers=bearer_for(B_ADMIN), json={"title": "T", "author": "A", "isbn": isbn,
                                                                     "copies_count": 2})
        assert res_b.status_code == 201, res_b.get_json()
        for bad in ("3", -5, 0, 100000, 1.5, True):
            assert client.post(url, headers=h, json={"title": "T", "author": "A", "isbn": f"X{uuid.uuid4().hex[:8]}",
                                                     "copies_count": bad}).status_code == 400
        assert client.post(url, headers=h, json={"title": "T", "author": "A", "isbn": 12345}).status_code == 400
    finally:
        db.session.rollback()
        for b in Book.query.filter(Book.isbn == isbn).all():
            db.session.delete(b)
        db.session.commit()


def test_library_issue_validation_scoping_and_actor(client, inst_b):
    inst = _inst_a()
    isbn = f"FIX-{uuid.uuid4().hex[:8]}"
    h = bearer_for(TEACHER)
    teacher = User.query.filter_by(email=TEACHER).first()
    borrower = User.query.filter_by(email=TEACHER2).first()
    add = client.post("/api/v1/library/books", headers=h, json={"title": "T", "author": "A", "isbn": isbn})
    assert add.status_code == 201
    book_id = uuid.UUID(add.get_json()["data"]["id"])
    copy_id = str(BookCopy.query.filter_by(book_id=book_id).first().id)
    url = "/api/v1/library/loans/issue"
    try:
        assert client.post(url, headers=h, json={"copy_id": "bad", "borrower_user_id": str(borrower.id)}).status_code == 400
        assert client.post(url, headers=h, json={"copy_id": copy_id, "borrower_user_id": str(uuid.uuid4())}).status_code == 404
        b_user = User.query.filter_by(email=B_ADMIN).first()
        assert client.post(url, headers=h, json={"copy_id": copy_id, "borrower_user_id": str(b_user.id)}).status_code == 404
        for bad in ("7", -1, 0, 10000):
            assert client.post(url, headers=h, json={"copy_id": copy_id, "borrower_user_id": str(borrower.id),
                                                     "duration_days": bad}).status_code == 400
        # Another tenant cannot issue St. Xavier's copy.
        assert client.post(url, headers=bearer_for(B_ADMIN),
                           json={"copy_id": copy_id, "borrower_user_id": str(b_user.id)}).status_code == 404

        ok = client.post(url, headers=h, json={"copy_id": copy_id, "borrower_user_id": str(borrower.id)})
        assert ok.status_code == 201, ok.get_json()
        loan = db.session.get(BookLoan, uuid.UUID(ok.get_json()["data"]["loan_id"]))
        assert loan.issued_by == teacher.id

        loans_b = client.get("/api/v1/library/loans", headers=bearer_for(B_ADMIN)).get_json()["data"]
        assert all(l["copy_id"] != copy_id for l in loans_b)
        loans_a = client.get("/api/v1/library/loans", headers=h).get_json()["data"]
        assert any(l["copy_id"] == copy_id for l in loans_a)
        assert client.post(f"/api/v1/library/loans/{loan.id}/return", headers=bearer_for(B_ADMIN)).status_code == 404
    finally:
        db.session.rollback()
        BookLoan.query.filter(BookLoan.copy_id == uuid.UUID(copy_id)).delete()
        db.session.delete(db.session.get(Book, book_id))
        db.session.commit()


# ---------------------------------------------------------------- admissions

def _application_payload(**extra):
    return {"first_name": "Fix", "last_name": "Mod", "date_of_birth": "2015-01-01", "gender": "Female", **extra}


def test_admissions_validation_unique_numbers_and_transitions(client, inst_b):
    h = admin_headers()
    url = "/api/v1/admissions/applications"
    created = []
    try:
        for bad in ({"date_of_birth": "01-01-2015"}, {"gender": 5}, {"target_class_level_id": "bad"},
                    {"target_class_level_id": str(uuid.uuid4())}, {"first_name": 7}):
            assert client.post(url, headers=h, json=_application_payload(**bad)).status_code in (400, 404), bad
        r1 = client.post(url, headers=h, json=_application_payload())
        r2 = client.post(url, headers=h, json=_application_payload())
        assert r1.status_code == 201 and r2.status_code == 201
        created = [r1.get_json()["data"]["id"], r2.get_json()["data"]["id"]]
        assert r1.get_json()["data"]["application_number"] != r2.get_json()["data"]["application_number"]

        status_url = f"/api/v1/admissions/applications/{created[0]}/status"
        assert client.put(status_url, headers=bearer_for(B_ADMIN), json={"status": "OFFERED"}).status_code == 404
        assert client.put(status_url, headers=h, json={"status": None}).status_code == 400
        assert client.put(status_url, headers=h, json={"status": "BOGUS"}).status_code == 400
        assert client.put(status_url, headers=h, json={"status": "ENROLLED"}).status_code == 409
        assert client.put(status_url, headers=h, json={"status": "OFFERED"}).status_code == 200
        assert client.put(status_url, headers=h, json={"status": "ENROLLED"}).status_code == 200
        assert client.put(status_url, headers=h, json={"status": "SUBMITTED"}).status_code == 409
    finally:
        db.session.rollback()
        for app_id in created:
            AdmissionApplication.query.filter_by(id=uuid.UUID(app_id)).delete()
        db.session.commit()


def test_inquiry_input_validation_and_class_autocreate_bumps_cache(client, inst_b):
    url = "/api/v1/admissions/inquiries"
    assert client.post(url, json={"phone": 9876543210}).status_code == 400
    assert client.post(url, json={"phone": "98765", "class_level_id": "nope"}).status_code == 400
    assert client.post(url, json={"phone": "98765", "dob": "2015/01/01"}).status_code == 400
    with patch("app.api.v1.admissions.bump_version") as bump:
        res = client.post(url, headers={"X-Institution-Id": str(inst_b.id)}, json={"phone": "98765"})
    assert res.status_code == 201, res.get_json()
    assert ClassLevel.query.filter_by(institution_id=inst_b.id).count() == 1
    bump.assert_called_once_with("academics", str(inst_b.id))


# ---------------------------------------------------------------- question papers

def test_question_paper_create_workflow_and_scoping(client, inst_b):
    inst = _inst_a()
    subject = Subject.query.filter_by(institution_id=inst.id).first()
    cls = ClassLevel.query.filter_by(institution_id=inst.id).first()
    teacher_h = bearer_for(TEACHER)
    content = {"sections": [{"q": "2+2?"}]}
    res = client.post("/api/v1/question-papers", headers=teacher_h, json={
        "title": "FixMod Paper", "subject_id": str(subject.id), "class_level_id": str(cls.id),
        "content_json": content})
    assert res.status_code == 201, res.get_json()
    paper_id = uuid.UUID(res.get_json()["data"]["id"])
    try:
        version = QuestionPaperVersion.query.filter_by(paper_id=paper_id).one()
        assert json.loads(version.content_text) == content
        teacher = User.query.filter_by(email=TEACHER).first()
        assert db.session.get(QuestionPaper, paper_id).author_teacher_id == teacher.id

        surl = f"/api/v1/question-papers/{paper_id}/status"
        assert client.get(f"/api/v1/question-papers/{paper_id}", headers=bearer_for(B_ADMIN)).status_code == 404
        assert client.put(surl, headers=bearer_for(B_ADMIN), json={"status": "SUBMITTED"}).status_code == 404
        assert client.put(surl, headers=teacher_h, json={"status": None}).status_code == 400
        assert client.put(surl, headers=teacher_h, json={"status": "APPROVED"}).status_code == 409  # DRAFT->APPROVED
        assert client.put(surl, headers=bearer_for(TEACHER2), json={"status": "SUBMITTED"}).status_code == 403
        assert client.put(surl, headers=teacher_h, json={"status": "SUBMITTED"}).status_code == 200
        assert client.put(surl, headers=teacher_h, json={"status": "APPROVED"}).status_code == 403  # self-approve
        assert client.put(surl, headers=admin_headers(), json={"status": "APPROVED"}).status_code == 200
        assert client.put(surl, headers=admin_headers(), json={"status": "DRAFT"}).status_code == 409
    finally:
        db.session.rollback()
        db.session.delete(db.session.get(QuestionPaper, paper_id))
        db.session.commit()


def test_question_paper_rejects_foreign_or_bad_ids(client, inst_b):
    inst = _inst_a()
    subject = Subject.query.filter_by(institution_id=inst.id).first()
    cls = ClassLevel.query.filter_by(institution_id=inst.id).first()
    url = "/api/v1/question-papers"
    assert client.post(url, headers=admin_headers(), json={
        "title": "x", "subject_id": "bad", "class_level_id": str(cls.id)}).status_code == 400
    assert client.post(url, headers=bearer_for(B_ADMIN), json={
        "title": "x", "subject_id": str(subject.id), "class_level_id": str(cls.id)}).status_code == 404


# ---------------------------------------------------------------- communications

def test_messages_validation_and_mailbox_privacy(client):
    principal = User.query.filter_by(email=PRINCIPAL).first()
    parent_h = bearer_for(PARENT)
    url = "/api/v1/communications/messages"
    assert client.get(f"{url}?user_id=bad", headers=admin_headers()).status_code == 400
    assert client.get(f"{url}?user_id={principal.id}", headers=parent_h).status_code == 403
    assert client.get(url, headers=parent_h).status_code == 200
    assert client.get(f"{url}?user_id={principal.id}", headers=admin_headers()).status_code == 200
    assert client.post(url, headers=parent_h, json={"recipient_id": "bad", "body": "hi"}).status_code == 400
    assert client.post(url, headers=parent_h, json={"recipient_id": str(uuid.uuid4()), "body": "hi"}).status_code == 404

    res = client.post(url, headers=parent_h, json={"recipient_id": str(principal.id), "body": "hello"})
    assert res.status_code == 201
    msg = db.session.get(Message, uuid.UUID(res.get_json()["data"]["id"]))
    try:
        assert msg.sender_id == User.query.filter_by(email=PARENT).first().id
    finally:
        db.session.delete(msg)
        db.session.commit()


def test_notice_and_credentials_input_validation(client):
    h = admin_headers()
    assert client.post("/api/v1/communications/notices", headers=h,
                       json={"title": "t", "content": "c", "priority": 3}).status_code == 400
    assert client.post("/api/v1/communications/send-credentials", headers=h,
                       json={"name": 5, "login_id": "x", "temporary_password": "y"}).status_code == 400


def test_send_credentials_escapes_html(client):
    captured = {}

    def fake_send(to, subject, html_body, text_body):
        captured["html"] = html_body
        return True

    with patch("app.api.v1.communications.email_service._send_email", side_effect=fake_send):
        res = client.post("/api/v1/communications/send-credentials", headers=admin_headers(), json={
            "name": "<script>alert(1)</script>", "login_id": "<b>id</b>", "temporary_password": "p&w",
            "email": "nobody@example.test", "channels": ["email"]})
    assert res.status_code == 200
    assert "<script>" not in captured["html"] and "&lt;script&gt;" in captured["html"]
    assert "<b>id</b>" not in captured["html"] and "p&amp;w" in captured["html"]


# ---------------------------------------------------------------- examinations / transport / system

def test_exam_results_bad_ids_and_tenant_scope(client, inst_b):
    h = admin_headers()
    assert client.get("/api/v1/examinations/results?exam_id=bad", headers=h).status_code == 400
    assert client.get("/api/v1/examinations/results?student_id=bad", headers=h).status_code == 400
    assert client.get("/api/v1/examinations/results", headers=h).status_code == 200
    assert client.get("/api/v1/examinations/results", headers=bearer_for(B_ADMIN)).get_json()["data"] == []


def test_active_trips_tenant_scoped(client, inst_b):
    assert client.get("/api/v1/transport/trips/active", headers=bearer_for(B_ADMIN)).get_json()["data"] == []


def test_health_does_not_leak_db_error(client):
    with patch("app.api.v1.system.db.session.execute", side_effect=Exception("password=hunter2 host=db.internal")):
        res = client.get("/api/v1/health")
    body = res.get_data(as_text=True)
    assert res.status_code == 503
    assert "hunter2" not in body and "db.internal" not in body


# ---------------------------------------------------------------- cache helpers

def test_cache_never_stores_tuple_error_responses(app):
    from app.utils.cache_helpers import _is_cacheable
    with app.test_request_context():
        resp = jsonify({"ok": True})
        assert _is_cacheable(resp) is True
        assert _is_cacheable((resp, 200)) is True
        assert _is_cacheable((resp, 404)) is False
        assert _is_cacheable((resp, 503, {"X": "y"})) is False


# ---------------------------------------------------------------- sockets

def _token_for(email, **overrides):
    user = User.query.filter_by(email=email).first()
    claims = {"institution_id": str(user.institution_id) if user.institution_id else None,
              "roles": user.role_names, "type": "access", "jti": str(uuid.uuid4())}
    claims.update(overrides)
    return create_access_token(identity=str(user.id), additional_claims=claims), claims["jti"], user


def _events(sc, name):
    return [e for e in sc.get_received() if e["name"] == name]


def test_socket_requires_valid_unrevoked_token(app):
    assert not socketio.test_client(app).is_connected()
    assert not socketio.test_client(app, auth={"token": "garbage"}).is_connected()

    token, jti, user = _token_for(PRINCIPAL)
    sc = socketio.test_client(app, auth={"token": token})
    assert sc.is_connected()
    sc.disconnect()

    db.session.add(TokenBlocklist(jti=jti, user_id=user.id, token_type="access",
                                  expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    db.session.commit()
    try:
        assert not socketio.test_client(app, auth={"token": token}).is_connected()
    finally:
        TokenBlocklist.query.filter_by(jti=jti).delete()
        db.session.commit()


def test_socket_rooms_come_from_token_not_client(app, inst_b):
    admin = socketio.test_client(app, auth={"token": _token_for(PRINCIPAL)[0]})
    teacher = socketio.test_client(app, auth={"token": _token_for(TEACHER)[0]})
    parent = socketio.test_client(app, query_string=f"token={_token_for(PARENT)[0]}")
    other = socketio.test_client(app, auth={"token": _token_for(B_ADMIN)[0]})
    try:
        for sc in (admin, teacher, parent, other):
            assert sc.is_connected()
            sc.get_received()

        # Cannot join another institution's room by naming it.
        other.emit("join_institution", {"institution_id": str(_inst_a().id)})
        assert _events(other, "error")

        # Client-supplied institution_id is ignored: the event goes to the sender's own room.
        admin.emit("notice_broadcast", {"title": "Fire drill", "institution_id": str(inst_b.id)})
        got = _events(teacher, "new_notice_published")
        assert got and got[0]["args"][0]["institution_id"] == str(_inst_a().id)
        assert _events(other, "new_notice_published") == []

        # Parents cannot push notices.
        parent.emit("notice_broadcast", {"title": "spoof"})
        assert _events(parent, "error")
        assert _events(teacher, "new_notice_published") == []
    finally:
        for sc in (admin, teacher, parent, other):
            if sc.is_connected():
                sc.disconnect()
