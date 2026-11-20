"""Regression tests for defects found during the full functional test pass."""
import uuid
from app.extensions import db
from app.models import User, Section, ClassLevel


PASSWORD = "SchoolERP@Demo2026!"


def test_login_resolves_exact_email_over_alias(client, app):
    """An identifier that also matches an alias account must log into the exact match.

    orders@apexsupplies.in aliases to vendor@apexsupplies.com and both users exist,
    so the lookup matched two rows and `.first()` could pick either one: a
    deactivated account could still log in as its active alias.
    """
    with app.app_context():
        user = User.query.filter_by(email="orders@apexsupplies.in").first()
        user.is_active = False
        db.session.commit()
    try:
        res = client.post("/api/v1/auth/login", json={"email": "orders@apexsupplies.in", "password": PASSWORD})
        assert res.status_code == 403
        assert res.get_json()["error"]["code"] == "ACCOUNT_INACTIVE"
    finally:
        with app.app_context():
            user = User.query.filter_by(email="orders@apexsupplies.in").first()
            user.is_active = True
            db.session.commit()

    res = client.post("/api/v1/auth/login", json={"email": "orders@apexsupplies.in", "password": PASSWORD})
    assert res.status_code == 200
    assert res.get_json()["data"]["user"]["email"] == "orders@apexsupplies.in"


def test_email_with_digits_never_matches_a_phone(client):
    """Digits inside an email must not be treated as a phone-number login."""
    res = client.post("/api/v1/auth/login", json={
        "email": "nobody9811234567@example.com",  # digits match Vikram Patel's phone
        "password": PASSWORD,
    })
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_class_teacher_can_access_own_section(client, app, auth_headers):
    """A section's class teacher manages it even without a subject allocation there."""
    with app.app_context():
        teacher = User.query.filter_by(email="priya.nair@stxaviers.edu.in").first()
        section = (
            Section.query.join(ClassLevel)
            .filter(Section.name == "A", ClassLevel.name == "Class 7")
            .first()
        )
        previous = section.class_teacher_id
        section.class_teacher_id = teacher.id
        db.session.commit()
        section_id = str(section.id)

    try:
        headers = auth_headers("priya.nair@stxaviers.edu.in")
        res = client.get(f"/api/v1/test/teacher-class/{section_id}", headers=headers)
        assert res.status_code == 200
    finally:
        with app.app_context():
            section = db.session.get(Section, uuid.UUID(section_id))
            section.class_teacher_id = previous
            db.session.commit()


def test_student_profile_only_visible_to_own_family(client, app):
    """Parents/students may open only their own record; staff may open any."""
    from tests._auth import admin_headers, bearer_for
    from app.models import Student, StudentParent, Parent

    with app.app_context():
        parent_user = User.query.filter_by(email="vikram.patel@gmail.com").first()
        parent = Parent.query.filter_by(user_id=parent_user.id).first()
        ward_ids = {sp.student_id for sp in StudentParent.query.filter_by(parent_id=parent.id)}
        own = Student.query.filter(Student.id.in_(ward_ids), Student.is_deleted.is_(False)).first()
        stranger = Student.query.filter(~Student.id.in_(ward_ids), Student.is_deleted.is_(False)).first()
        own_student_user = db.session.get(User, own.user_id)
        own_id, stranger_id = str(own.id), str(stranger.id) if stranger else None
        parent_headers = bearer_for("vikram.patel@gmail.com")
        student_headers = bearer_for(own_student_user.email)
        staff_headers = admin_headers()

    assert client.get(f"/api/v1/students/{own_id}", headers=parent_headers).status_code == 200
    assert client.get(f"/api/v1/students/{own_id}", headers=student_headers).status_code == 200
    assert client.get(f"/api/v1/students/{own_id}", headers=staff_headers).status_code == 200
    if stranger_id:
        assert client.get(f"/api/v1/students/{stranger_id}", headers=parent_headers).status_code == 404
        assert client.get(f"/api/v1/students/{stranger_id}", headers=student_headers).status_code == 404


def test_sync_store_only_returns_callers_own_students(client, app):
    """Students/parents hydrate from /sync/store but must not get the whole school."""
    from tests._auth import admin_headers, bearer_for
    from app.models import Student, StudentParent, Parent

    with app.app_context():
        parent_user = User.query.filter_by(email="vikram.patel@gmail.com").first()
        parent = Parent.query.filter_by(user_id=parent_user.id).first()
        ward_ids = {str(sp.student_id) for sp in StudentParent.query.filter_by(parent_id=parent.id)}
        student_user = User.query.filter_by(email="aarav.sharma@student.stxaviers.edu.in").first()
        own_student = Student.query.filter_by(user_id=student_user.id).first()
        own_id = str(own_student.id)
        parent_headers = bearer_for("vikram.patel@gmail.com")
        student_headers = bearer_for("aarav.sharma@student.stxaviers.edu.in")
        supplier_headers = bearer_for("orders@apexsupplies.in")
        staff_headers = admin_headers()

    staff_ids = {s["id"] for s in client.get("/api/v1/sync/store", headers=staff_headers).get_json()["data"]["students"]}
    student_ids = {s["id"] for s in client.get("/api/v1/sync/store", headers=student_headers).get_json()["data"]["students"]}
    parent_ids = {s["id"] for s in client.get("/api/v1/sync/store", headers=parent_headers).get_json()["data"]["students"]}
    supplier_data = client.get("/api/v1/sync/store", headers=supplier_headers).get_json()["data"]

    assert len(staff_ids) > 1
    assert student_ids == {own_id}
    assert parent_ids and parent_ids <= ward_ids
    assert supplier_data["students"] == [] and supplier_data["feeInvoices"] == []
