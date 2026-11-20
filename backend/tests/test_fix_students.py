"""Regression tests for the student / guardian / sync audit fixes."""
import uuid
from datetime import date

import pytest

from app.extensions import db
from app.models import (
    User, Student, Section, ClassLevel, Institution, StudentEnrollment, StudentHistory,
    StaffProfile, FeeInvoice,
)
from tests._auth import admin_headers, bearer_for

TEACHER = "priya.nair@stxaviers.edu.in"


@pytest.fixture
def admin(app):
    with app.app_context():
        return admin_headers()


@pytest.fixture
def teacher(app):
    with app.app_context():
        return bearer_for(TEACHER)


@pytest.fixture
def section_id(app):
    with app.app_context():
        inst = Institution.query.filter_by(code="STXAV-DELHI").first()
        sec = Section.query.join(ClassLevel).filter(ClassLevel.institution_id == inst.id).first()
        return str(sec.id)


def _uniq(prefix="FIX"):
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _create(client, admin, section_id, **extra):
    body = {"admission_number": _uniq(), "first_name": "Fix", "last_name": "Student",
            "section_id": section_id, **extra}
    res = client.post("/api/v1/students", json=body, headers=admin)
    assert res.status_code == 201, res.get_json()
    return res.get_json()["data"]


# ---- 1. never reuse an existing login --------------------------------------

def test_create_with_existing_users_email_is_409_and_teacher_untouched(client, app, admin, section_id):
    res = client.post("/api/v1/students", json={
        "admission_number": _uniq(), "first_name": "X", "section_id": section_id,
        "email": TEACHER.upper(),
    }, headers=admin)
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "EMAIL_IN_USE"
    with app.app_context():
        teacher = User.query.filter_by(email=TEACHER).first()
        assert teacher.is_active
        assert Student.query.filter_by(user_id=teacher.id).first() is None


def test_create_with_new_email_creates_own_login(client, app, admin, section_id):
    email = f"{uuid.uuid4().hex[:10]}@student.example.com"
    data = _create(client, admin, section_id, email=email)
    assert data["email"] == email


# ---- 2 / 8. duplicate admission numbers -------------------------------------

def test_duplicate_admission_number_is_case_insensitive(client, admin, section_id):
    data = _create(client, admin, section_id, admission_number=_uniq("dupcase").lower())
    res = client.post("/api/v1/students", json={
        "admission_number": data["admission_number"].upper(), "section_id": section_id,
    }, headers=admin)
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "DUPLICATE_ADMISSION_NO"


# ---- 3. create input validation ---------------------------------------------

@pytest.mark.parametrize("extra, code", [
    ({"dob": "14/08/2010"}, "INVALID_DATE"),
    ({"admission_date": "not-a-date"}, "INVALID_DATE"),
    ({"dob": 20100814}, "INVALID_DATE"),
    ({"blood_group": "X" * 11}, "FIELD_TOO_LONG"),
    ({"admission_number": "A" * 51}, "FIELD_TOO_LONG"),
    ({"gender": {"a": 1}}, "VALIDATION_ERROR"),
    ({"email": "not-an-email"}, "INVALID_EMAIL"),
])
def test_create_bad_input_is_400(client, admin, section_id, extra, code):
    body = {"admission_number": _uniq(), "section_id": section_id, **extra}
    res = client.post("/api/v1/students", json=body, headers=admin)
    assert res.status_code == 400, res.get_json()
    assert res.get_json()["error"]["code"] == code


def test_create_list_body_is_400(client, admin):
    res = client.post("/api/v1/students", json=[{"admission_number": "X"}], headers=admin)
    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "INVALID_BODY"


def test_create_int_admission_number_and_null_gender(client, admin, section_id):
    adm = int(uuid.uuid4().int % 10**9)
    data = _create(client, admin, section_id, admission_number=adm, gender=None)
    assert data["admission_number"] == str(adm)
    assert data["gender"] == "OTHER"


# ---- 4 / 5. update validation, deleted students, status side effects --------

@pytest.mark.parametrize("body, code", [
    ({"gender": None}, "MISSING_FIELD"),
    ({"status": 5}, "INVALID_STATUS"),
    ({"status": None}, "MISSING_FIELD"),
    ({"status": "ON_HOLIDAY"}, "INVALID_STATUS"),
    ({"first_name": ""}, "MISSING_FIELD"),
    ({"blood_group": "Y" * 11}, "FIELD_TOO_LONG"),
])
def test_update_bad_input_is_400(client, admin, section_id, body, code):
    sid = _create(client, admin, section_id, email=f"{uuid.uuid4().hex[:10]}@student.example.com")["id"]
    res = client.patch(f"/api/v1/students/{sid}", json=body, headers=admin)
    assert res.status_code == 400, res.get_json()
    assert res.get_json()["error"]["code"] == code


def test_update_deleted_student_is_404(client, app, admin, section_id):
    sid = _create(client, admin, section_id)["id"]
    assert client.delete(f"/api/v1/students/{sid}", headers=admin).status_code == 200
    res = client.patch(f"/api/v1/students/{sid}", json={"status": "ACTIVE"}, headers=admin)
    assert res.status_code == 404
    with app.app_context():
        s = db.session.get(Student, uuid.UUID(sid))
        assert s.is_deleted and s.status == "WITHDRAWN"


def test_withdraw_revokes_login_and_reactivate_restores(client, app, admin, section_id):
    sid = _create(client, admin, section_id, email=f"{uuid.uuid4().hex[:10]}@student.example.com")["id"]
    res = client.patch(f"/api/v1/students/{sid}", json={"status": "withdrawn"}, headers=admin)
    assert res.status_code == 200
    with app.app_context():
        s = db.session.get(Student, uuid.UUID(sid))
        assert s.status == "WITHDRAWN"
        assert s.user.is_active is False
        assert all(e.status != "ACTIVE" for e in s.enrollments)

    res = client.patch(f"/api/v1/students/{sid}", json={"status": "ACTIVE"}, headers=admin)
    assert res.status_code == 200
    with app.app_context():
        s = db.session.get(Student, uuid.UUID(sid))
        assert s.status == "ACTIVE"
        assert s.user.is_active is True
        assert any(e.status == "ACTIVE" for e in s.enrollments)


# ---- 6. restore + permanent delete --------------------------------------------

def test_restore_deleted_student(client, app, admin, section_id):
    sid = _create(client, admin, section_id, email=f"{uuid.uuid4().hex[:10]}@student.example.com")["id"]
    # Restoring a live student is a conflict.
    assert client.post(f"/api/v1/students/{sid}/restore", headers=admin).status_code == 409
    assert client.delete(f"/api/v1/students/{sid}", headers=admin).status_code == 200

    res = client.post(f"/api/v1/students/{sid}/restore", headers=admin)
    assert res.status_code == 200, res.get_json()
    with app.app_context():
        s = db.session.get(Student, uuid.UUID(sid))
        assert not s.is_deleted and s.status == "ACTIVE"
        assert s.user.is_active and not s.user.is_deleted
        assert any(e.status == "ACTIVE" for e in s.enrollments)
        ops = [h.operation for h in StudentHistory.query.filter_by(student_id=s.id)
               .order_by(StudentHistory.version_number).all()]
        assert ops[-1] == "RESTORE"
    assert client.get(f"/api/v1/students/{sid}", headers=admin).status_code == 200


def test_restore_and_purge_require_school_admin(client, admin, teacher, section_id):
    sid = _create(client, admin, section_id)["id"]
    assert client.post(f"/api/v1/students/{sid}/restore", headers=teacher).status_code == 403
    assert client.delete(f"/api/v1/students/{sid}/permanent", headers=teacher).status_code == 403
    assert client.post(f"/api/v1/students/{sid}/restore").status_code == 401


def test_permanent_delete_only_after_soft_delete(client, app, admin, section_id):
    email = f"{uuid.uuid4().hex[:10]}@student.example.com"
    sid = _create(client, admin, section_id, email=email)["id"]
    res = client.delete(f"/api/v1/students/{sid}/permanent", headers=admin)
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "STUDENT_NOT_DELETED"

    assert client.delete(f"/api/v1/students/{sid}", headers=admin).status_code == 200
    res = client.delete(f"/api/v1/students/{sid}/permanent", headers=admin)
    assert res.status_code == 200, res.get_json()
    with app.app_context():
        assert db.session.get(Student, uuid.UUID(sid)) is None
        assert StudentEnrollment.query.filter_by(student_id=uuid.UUID(sid)).count() == 0
        assert User.query.filter_by(email=email).first() is None
    assert client.delete(f"/api/v1/students/{sid}/permanent", headers=admin).status_code == 404


def test_permanent_delete_blocked_by_financial_records(client, app, admin):
    with app.app_context():
        inv = FeeInvoice.query.first()
        if inv is None:
            pytest.skip("seed has no fee invoices")
        student = db.session.get(Student, inv.student_id)
        sid, was_deleted = str(student.id), student.is_deleted
        student.is_deleted = True
        db.session.commit()
    try:
        res = client.delete(f"/api/v1/students/{sid}/permanent", headers=admin)
        assert res.status_code == 409
        assert res.get_json()["error"]["code"] == "STUDENT_HAS_RECORDS"
        with app.app_context():
            assert db.session.get(Student, uuid.UUID(sid)) is not None
    finally:
        with app.app_context():
            student = db.session.get(Student, uuid.UUID(sid))
            student.is_deleted = was_deleted
            db.session.commit()


# ---- 7. list parameter hardening -----------------------------------------------

def test_list_bad_section_id_is_400(client, admin):
    res = client.get("/api/v1/students?section_id=not-a-uuid", headers=admin)
    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "INVALID_SECTION_ID"


@pytest.mark.parametrize("url", ["/api/v1/students", "/api/v1/guardians"])
def test_huge_page_number_does_not_500(client, admin, url):
    res = client.get(f"{url}?page=99999999999999999999", headers=admin)
    assert res.status_code == 200
    assert res.get_json()["data"]["items"] == []


# ---- 9. bulk import robustness ----------------------------------------------------

def test_bulk_import_tolerates_malformed_rows(client, app, admin, section_id):
    adm = int(uuid.uuid4().int % 10**9)
    res = client.post("/api/v1/students/bulk-import", json={
        "rowOffset": "abc",
        "students": [
            "not-a-dict",
            42,
            {"fullName": 12345, "admissionNo": adm, "section_id": section_id},
            {"fullName": "Too Long", "admissionNo": "Z" * 60, "section_id": section_id},
        ],
    }, headers=admin)
    assert res.status_code == 201, res.get_json()
    data = res.get_json()["data"]
    assert data["imported"] == 1
    assert data["created"][0]["admissionNo"] == str(adm)
    reasons = {e["row"]: e["reason"] for e in data["errors"]}
    assert reasons[2] == "Row is not a valid record" and reasons[3] == "Row is not a valid record"
    assert "too long" in reasons[5]


def test_bulk_import_list_body_is_400(client, admin):
    res = client.post("/api/v1/students/bulk-import", json=[{"fullName": "A"}], headers=admin)
    assert res.status_code == 400


# ---- 10. import lookups are not cached --------------------------------------------

def test_bulk_import_sees_newly_created_section(client, app, admin, section_id):
    # Warm whatever lookup path exists, then add a brand new section.
    client.post("/api/v1/students/bulk-import", json={"students": [
        {"fullName": "Warm Up", "admissionNo": _uniq(), "section_id": section_id}]}, headers=admin)
    with app.app_context():
        base = db.session.get(Section, uuid.UUID(section_id))
        new_name = "Q" + uuid.uuid4().hex[:3].upper()
        sec = Section(class_level_id=base.class_level_id, name=new_name, capacity=40)
        db.session.add(sec)
        db.session.commit()
        cls_name = base.class_level.name
    res = client.post("/api/v1/students/bulk-import", json={"students": [
        {"fullName": "Fresh Section", "admissionNo": _uniq(), "className": cls_name, "section": new_name}]},
        headers=admin)
    assert res.status_code == 201
    assert res.get_json()["data"]["imported"] == 1, res.get_json()


# ---- 11. sync store tenant scoping & no writes ---------------------------------------

def test_sync_store_get_is_tenant_scoped(client, app, admin):
    with app.app_context():
        other = Institution(code=_uniq("OTH")[:30], name="Other School", email="o@other.example.com",
                            status="TRIAL")
        db.session.add(other)
        db.session.flush()
        foreign = Student(institution_id=other.id, admission_number=_uniq("FOREIGN"),
                          admission_date=date.today(), date_of_birth=date(2012, 1, 1),
                          gender="MALE", status="ACTIVE")
        db.session.add(foreign)
        db.session.commit()
        other_id, foreign_id = other.id, foreign.id
        own = Institution.query.filter_by(code="STXAV-DELHI").first().id
    try:
        data = client.get("/api/v1/sync/store", headers=admin).get_json()["data"]
        assert [i["id"] for i in data["institutions"]] == [str(own)]
        assert str(foreign_id) not in {s["id"] for s in data["students"]}
        for key in ("classes", "students", "teachers", "feeInvoices", "books", "notices", "transportRoutes"):
            assert all(row.get("institutionId") == str(own) for row in data[key]), key
    finally:
        with app.app_context():
            db.session.delete(db.session.get(Student, foreign_id))
            db.session.delete(db.session.get(Institution, other_id))
            db.session.commit()


def test_sync_store_post_never_creates_students(client, app, admin):
    adm = _uniq("SYNC")
    with app.app_context():
        before = Student.query.count()
    res = client.post("/api/v1/sync/store", json={"students": [
        {"admissionNo": adm, "firstName": "Ghost", "dob": "bad-date"},
        {"admissionNo": 12345},
        "junk",
    ]}, headers=admin)
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["data"]["created"] == 0
    with app.app_context():
        assert Student.query.count() == before
        assert Student.query.filter_by(admission_number=adm).first() is None
    assert client.post("/api/v1/sync/store", json=[1, 2], headers=admin).status_code == 400


# ---- 12. unassigned teacher gets no classes -----------------------------------------------

def test_unassigned_teacher_has_no_fallback_class(client, app, admin):
    with app.app_context():
        from app.models import TeacherAssignment
        inst = Institution.query.filter_by(code="STXAV-DELHI").first()
        unassigned = []
        for sp in StaffProfile.query.filter_by(institution_id=inst.id).all():
            if sp.user and not TeacherAssignment.query.filter_by(teacher_id=sp.user_id).first() \
                    and not Section.query.filter_by(class_teacher_id=sp.user_id).first():
                unassigned.append(str(sp.user_id))
    if not unassigned:
        pytest.skip("seed has no unassigned staff")
    teachers = {t["id"]: t for t in client.get("/api/v1/sync/store", headers=admin).get_json()["data"]["teachers"]}
    for uid in unassigned:
        assert teachers[uid]["assignedClasses"] == []
        assert teachers[uid]["classes"] == []


# ---- 13. deleted students excluded from guardian section filter ------------------------------

def test_guardian_section_filter_ignores_deleted_students(client, app, admin):
    from app.models import StudentParent
    with app.app_context():
        link = StudentParent.query.join(Student).filter(Student.is_deleted.is_(False)).first()
        if link is None:
            pytest.skip("seed has no guardian links")
        student = link.student
        enr = next((e for e in student.enrollments if e.status == "ACTIVE"), None)
        if enr is None:
            pytest.skip("linked student has no active enrollment")
        sec_id, parent_id, sid = str(enr.section_id), str(link.parent_id), student.id
        # Only this student links the parent to the section? Otherwise inconclusive.
        others = (StudentParent.query.join(Student).join(StudentEnrollment)
                  .filter(StudentParent.parent_id == link.parent_id, Student.id != sid,
                          StudentEnrollment.section_id == enr.section_id).count())
        student.is_deleted = True
        db.session.commit()
    try:
        items = client.get(f"/api/v1/guardians?section_id={sec_id}&limit=100", headers=admin).get_json()["data"]["items"]
        if not others:
            assert parent_id not in {g["id"] for g in items}
        for g in items:
            assert str(sid) not in {s["id"] for s in g["students"]}
    finally:
        with app.app_context():
            s = db.session.get(Student, sid)
            s.is_deleted = False
            db.session.commit()
