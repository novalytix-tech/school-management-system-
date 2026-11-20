"""Regression tests: tenant scoping, teacher access and input validation for
attendance, academics, staff leave actions and institution settings."""
import uuid
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import (
    Institution, User, Role, UserRole, ClassLevel, Section, Subject, AcademicYear,
    StudentEnrollment, StudentAttendance, LeaveCategory, LeaveRequest, Student,
)
from app.utils.security import hash_password
from tests._auth import admin_headers, bearer_for

PASSWORD = "SchoolERP@Demo2026!"
ADMIN = "principal@stxaviers.edu.in"
ANANYA = "ananya.sharma@stxaviers.edu.in"
PRIYA = "priya.nair@stxaviers.edu.in"
OTHER_ADMIN = "admin.fixacad@campus-b.test"

# A fixed past school day so tests never collide with "today" records of other tests.
PAST_DAY = date(2025, 1, 6)


def _st_xavier():
    return Institution.query.filter_by(code="STXAV-DELHI").first()


def _section_10a():
    inst = _st_xavier()
    return (
        Section.query.join(ClassLevel)
        .filter(ClassLevel.institution_id == inst.id, ClassLevel.name == "Class 10", Section.name == "A")
        .first()
    )


def _ensure_other_admin():
    inst_b = Institution.query.filter_by(code="FIXACAD-B").first()
    if not inst_b:
        inst_b = Institution(
            name="Fix Academics Campus B", code="FIXACAD-B", email="office@campus-b.test",
            board_affiliation="CBSE", currency="INR", status="ACTIVE",
        )
        db.session.add(inst_b)
        db.session.flush()
    user = User.query.filter_by(email=OTHER_ADMIN).first()
    if not user:
        user = User(
            institution_id=inst_b.id, username="admin_fixacad_b", email=OTHER_ADMIN,
            password_hash=hash_password(PASSWORD), first_name="Other", last_name="Admin", is_active=True,
        )
        db.session.add(user)
        db.session.flush()
        role = Role.query.filter_by(name="school_admin").first()
        db.session.add(UserRole(user_id=user.id, role_id=role.id))
    db.session.commit()


@pytest.fixture
def ctx(app):
    with app.app_context():
        _ensure_other_admin()
        sec = _section_10a()
        enr_10a = StudentEnrollment.query.filter_by(section_id=sec.id, status="ACTIVE").first()
        enr_other = (
            StudentEnrollment.query.join(Student)
            .filter(
                Student.institution_id == _st_xavier().id,
                StudentEnrollment.section_id != sec.id,
                StudentEnrollment.status == "ACTIVE",
            )
            .first()
        )
        data = {
            "section_10a": str(sec.id),
            "enr_10a": str(enr_10a.id),
            "enr_10a_student": str(enr_10a.student_id),
            "enr_other": str(enr_other.id),
            "admin": admin_headers(),
            "ananya": bearer_for(ANANYA),
            "ananya_id": User.query.filter_by(email=ANANYA).first().id,
            "priya": bearer_for(PRIYA),
            "other": bearer_for(OTHER_ADMIN),
        }
    yield data
    with app.app_context():
        StudentAttendance.query.filter(
            StudentAttendance.attendance_date.in_([PAST_DAY, PAST_DAY + timedelta(days=1)])
        ).delete(synchronize_session=False)
        db.session.commit()


def _batch(client, headers, records, day=PAST_DAY, **extra):
    body = {"date": day.isoformat() if isinstance(day, date) else day, "records": records}
    body.update(extra)
    return client.post("/api/v1/attendance/students/batch", json=body, headers=headers)


# -------------------------------------------------------------
# Finding 1: tenant scoping
# -------------------------------------------------------------

def test_attendance_and_section_are_tenant_scoped(client, ctx):
    res = _batch(client, ctx["admin"], [{"enrollment_id": ctx["enr_10a"], "status": "PRESENT"}])
    assert res.status_code == 200

    other = ctx["other"]
    # Another school's admin sees none of St Xavier's attendance...
    res = client.get(f"/api/v1/attendance/students?date={PAST_DAY.isoformat()}", headers=other)
    assert res.status_code == 200
    assert all(r["enrollment_id"] != ctx["enr_10a"] for r in res.get_json()["data"])
    # ...cannot filter by its section, read the section, or mark its students.
    res = client.get(f"/api/v1/attendance/students?section_id={ctx['section_10a']}", headers=other)
    assert res.status_code == 404
    assert client.get(f"/api/v1/academics/sections/{ctx['section_10a']}", headers=other).status_code == 404
    res = _batch(client, other, [{"enrollment_id": ctx["enr_10a"], "status": "ABSENT"}])
    assert res.status_code == 400
    res = _batch(client, other, [{"student_id": ctx["enr_10a_student"], "status": "ABSENT"}])
    assert res.status_code == 400
    res = _batch(client, other, [], section_id=ctx["section_10a"])
    assert res.status_code == 404

    # The St Xavier's record was not touched.
    res = client.get(f"/api/v1/attendance/students?date={PAST_DAY.isoformat()}", headers=ctx["admin"])
    rec = next(r for r in res.get_json()["data"] if r["enrollment_id"] == ctx["enr_10a"])
    assert rec["status"] == "PRESENT"
    # The owner can still read its own section.
    assert client.get(f"/api/v1/academics/sections/{ctx['section_10a']}", headers=ctx["admin"]).status_code == 200


# -------------------------------------------------------------
# Finding 2: teacher section access + recorder identity
# -------------------------------------------------------------

def test_teacher_can_only_mark_own_section(app, client, ctx):
    res = _batch(client, ctx["ananya"], [{"enrollment_id": ctx["enr_10a"], "status": "LATE"}],
                 section_id=ctx["section_10a"])
    assert res.status_code == 200
    with app.app_context():
        rec = StudentAttendance.query.filter_by(
            enrollment_id=uuid.UUID(ctx["enr_10a"]), attendance_date=PAST_DAY).first()
        assert rec.status == "LATE"
        assert rec.recorded_by == ctx["ananya_id"]

    # Section she does not teach -> 403
    res = _batch(client, ctx["ananya"], [{"enrollment_id": ctx["enr_other"], "status": "PRESENT"}])
    assert res.status_code == 403
    # Teacher with no allocation in 10-A -> 403
    res = _batch(client, ctx["priya"], [{"enrollment_id": ctx["enr_10a"], "status": "PRESENT"}])
    assert res.status_code == 403
    # Enrollment outside the declared section -> 400
    res = _batch(client, ctx["ananya"], [{"enrollment_id": ctx["enr_other"], "status": "PRESENT"}],
                 section_id=ctx["section_10a"])
    assert res.status_code == 400


# -------------------------------------------------------------
# Finding 3: batch validation
# -------------------------------------------------------------

@pytest.mark.parametrize("records,day", [
    ([{"enrollment_id": "E", "status": None}], PAST_DAY),
    ([{"enrollment_id": "E", "status": "X" * 50}], PAST_DAY),
    ([{"enrollment_id": "E", "status": "HOLIDAY"}], PAST_DAY),
    ([{"enrollment_id": "E", "status": "PRESENT"}], "2025-13-45"),
    ([{"enrollment_id": "E", "status": "PRESENT"}], "not-a-date"),
    ([{"enrollment_id": "E", "status": "PRESENT"}], date.today() + timedelta(days=3)),
    ([{"enrollment_id": "not-a-uuid", "status": "PRESENT"}], PAST_DAY),
    ([{"student_id": "not-a-uuid", "status": "PRESENT"}], PAST_DAY),
    ([{"enrollment_id": str(uuid.uuid4()), "status": "PRESENT"}], PAST_DAY),
    ([{"enrollment_id": "E", "status": "PRESENT"}, {"enrollment_id": "E", "status": "ABSENT"}], PAST_DAY),
    ([{"status": "PRESENT"}], PAST_DAY),
    (["E"], PAST_DAY),
    ({"enrollment_id": "E"}, PAST_DAY),
    ("records", PAST_DAY),
])
def test_attendance_batch_rejects_bad_input(client, ctx, records, day):
    def sub(v):
        if v == "E":
            return ctx["enr_10a"]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v

    res = _batch(client, ctx["admin"], sub(records), day=day)
    assert res.status_code == 400, res.get_json()


def test_attendance_get_rejects_bad_params(client, ctx):
    assert client.get("/api/v1/attendance/students?date=2025-99-99", headers=ctx["admin"]).status_code == 400
    assert client.get("/api/v1/attendance/students?section_id=nope", headers=ctx["admin"]).status_code == 400
    assert client.get("/api/v1/attendance/staff?date=garbage", headers=ctx["admin"]).status_code == 400


def test_attendance_remark_same_day_upserts(app, client, ctx):
    assert _batch(client, ctx["admin"], [{"enrollment_id": ctx["enr_10a"], "status": "present"}]).status_code == 200
    assert _batch(client, ctx["admin"], [{"enrollment_id": ctx["enr_10a"], "status": "EXCUSED",
                                          "remarks": "Doctor"}]).status_code == 200
    with app.app_context():
        rows = StudentAttendance.query.filter_by(
            enrollment_id=uuid.UUID(ctx["enr_10a"]), attendance_date=PAST_DAY).all()
        assert len(rows) == 1
        assert rows[0].status == "EXCUSED"


# -------------------------------------------------------------
# Findings 4 & 5: academics validation, duplicates, cache bump
# -------------------------------------------------------------

@pytest.fixture
def cleanup_academics(app):
    created = {"classes": [], "sessions": [], "subjects": []}
    yield created
    with app.app_context():
        for model, key in ((ClassLevel, "classes"), (AcademicYear, "sessions"), (Subject, "subjects")):
            for obj_id in created[key]:
                obj = db.session.get(model, uuid.UUID(obj_id))
                if obj:
                    db.session.delete(obj)
        db.session.commit()


@pytest.mark.parametrize("body", [
    {"name": "Class 10"},                       # duplicate (409 checked below)
    {"name": "FixT Dup Sections", "sections": ["A", "a"]},
    {"name": 123},
    {"name": "FixT Seq", "sequence_order": "abc"},
    {"name": "FixT Cap", "capacity": -5},
    {"name": "FixT Sections", "sections": "A"},
])
def test_create_class_rejects_bad_input(client, cleanup_academics, body):
    res = client.post("/api/v1/academics/classes", json=body, headers=admin_headers())
    if res.status_code in (200, 201):
        cleanup_academics["classes"].append(res.get_json()["data"]["id"])
    expected = 409 if body["name"] == "Class 10" else 400
    assert res.status_code == expected, res.get_json()


def test_create_class_bumps_cache(client, cleanup_academics):
    headers = admin_headers()
    before = client.get("/api/v1/academics/classes", headers=headers).get_json()["data"]
    assert all(c["name"] != "FixT Cache Class" for c in before)
    res = client.post("/api/v1/academics/classes", json={
        "name": "FixT Cache Class", "code": "FIXT-CACHE", "sequence_order": "77", "sections": ["a", "B"],
    }, headers=headers)
    assert res.status_code == 201, res.get_json()
    cleanup_academics["classes"].append(res.get_json()["data"]["id"])
    after = client.get("/api/v1/academics/classes", headers=headers).get_json()["data"]
    created = next(c for c in after if c["name"] == "FixT Cache Class")
    assert sorted(s["name"] for s in created["sections"]) == ["A", "B"]
    # Case-insensitive duplicate name -> 409
    res = client.post("/api/v1/academics/classes", json={"name": "fixt cache class", "code": "OTHER"},
                      headers=headers)
    assert res.status_code == 409


@pytest.mark.parametrize("body", [
    {"name": "FixT S1", "start_date": "2030-06-01", "end_date": "2030-01-01"},
    {"name": "FixT S2", "start_date": "2030-01-01", "end_date": "2030-01-01"},
    {"name": "FixT S3", "start_date": "2030-13-45", "end_date": "2031-01-01"},
    {"name": "FixT S4", "start_date": 20300101, "end_date": "2031-01-01"},
    {"name": 2030, "start_date": "2030-01-01", "end_date": "2031-01-01"},
    {"name": "FixT S5", "start_date": "2030-01-01", "end_date": "2031-01-01", "is_current": "yes"},
])
def test_create_session_rejects_bad_input(client, cleanup_academics, body):
    res = client.post("/api/v1/academics/sessions", json=body, headers=admin_headers())
    if res.status_code == 201:
        cleanup_academics["sessions"].append(res.get_json()["data"]["id"])
    assert res.status_code == 400, res.get_json()


def test_duplicate_session_name_conflicts(client, cleanup_academics):
    body = {"name": "FixT 2040-41", "start_date": "2040-04-01", "end_date": "2041-03-31"}
    res = client.post("/api/v1/academics/sessions", json=body, headers=admin_headers())
    assert res.status_code == 201
    cleanup_academics["sessions"].append(res.get_json()["data"]["id"])
    res = client.post("/api/v1/academics/sessions", json=body, headers=admin_headers())
    assert res.status_code == 409
    res = client.post("/api/v1/academics/sessions", json={**body, "name": " fixt 2040-41 "},
                      headers=admin_headers())
    assert res.status_code == 409


@pytest.mark.parametrize("body", [
    {"name": "FixT Sub", "code": "FIXT-1", "credit_hours": "abc"},
    {"name": "FixT Sub", "code": "FIXT-2", "subject_type": 5},
    {"name": "FixT Sub", "code": "FIXT-3", "credit_hours": -1},
    {"name": 42, "code": "FIXT-4"},
])
def test_create_subject_rejects_bad_input(client, cleanup_academics, body):
    res = client.post("/api/v1/academics/subjects", json=body, headers=admin_headers())
    if res.status_code == 201:
        cleanup_academics["subjects"].append(res.get_json()["data"]["id"])
    assert res.status_code == 400, res.get_json()


def test_subject_code_duplicate_is_normalised(client, cleanup_academics):
    res = client.post("/api/v1/academics/subjects", json={"name": "FixT Norm", "code": " fixt-norm "},
                      headers=admin_headers())
    assert res.status_code == 201
    assert res.get_json()["data"]["code"] == "FIXT-NORM"
    cleanup_academics["subjects"].append(res.get_json()["data"]["id"])
    res = client.post("/api/v1/academics/subjects", json={"name": "FixT Norm 2", "code": "Fixt-Norm"},
                      headers=admin_headers())
    assert res.status_code == 409


def test_timetable_bad_section_id_is_400(client):
    res = client.get("/api/v1/academics/timetable?section_id=xyz", headers=admin_headers())
    assert res.status_code == 400


# -------------------------------------------------------------
# Finding 6: leave action
# -------------------------------------------------------------

def test_leave_action(app, client, ctx):
    with app.app_context():
        inst = _st_xavier()
        cat = LeaveCategory.query.filter_by(institution_id=inst.id).first()
        if not cat:
            cat = LeaveCategory(institution_id=inst.id, name="FixT Leave", days_allowed_per_year=5)
            db.session.add(cat)
            db.session.flush()
        applicant = User.query.filter_by(email=PRIYA).first()
        leave = LeaveRequest(
            institution_id=inst.id, applicant_user_id=applicant.id, leave_category_id=cat.id,
            start_date=date(2030, 1, 1), end_date=date(2030, 1, 2), reason="FixT", status="PENDING",
        )
        db.session.add(leave)
        db.session.commit()
        leave_id = str(leave.id)
        principal_id = User.query.filter_by(email=ADMIN).first().id
    url = f"/api/v1/staff/leaves/{leave_id}/action"
    try:
        assert client.put(url, json={"action": "APPROVE"}, headers=ctx["other"]).status_code == 404
        assert client.put(url, json={"action": 5}, headers=ctx["admin"]).status_code == 400
        assert client.put(url, json={"action": "MAYBE"}, headers=ctx["admin"]).status_code == 400
        assert client.put(url, json=["APPROVE"], headers=ctx["admin"]).status_code == 400
        res = client.put(url, json={"action": "APPROVE", "comments": "ok"}, headers=ctx["admin"])
        assert res.status_code == 200, res.get_json()
        assert res.get_json()["data"]["status"] == "APPROVED"
        with app.app_context():
            saved = db.session.get(LeaveRequest, uuid.UUID(leave_id))
            assert saved.approval.approver_user_id == principal_id
            assert saved.approval.remarks == "ok"
        assert client.put(url, json={"action": "REJECT"}, headers=ctx["admin"]).status_code == 409
    finally:
        with app.app_context():
            obj = db.session.get(LeaveRequest, uuid.UUID(leave_id))
            if obj:
                db.session.delete(obj)
                db.session.commit()


# -------------------------------------------------------------
# Finding 7: institution settings body validation
# -------------------------------------------------------------

def test_institution_settings_rejects_bad_body(client):
    headers = admin_headers()
    url = "/api/v1/institutions/current/settings"
    assert client.put(url, json=["a", "b"], headers=headers).status_code == 400
    assert client.put(url, json="text", headers=headers).status_code == 400
    assert client.put(url, json={"k" * 101: "v"}, headers=headers).status_code == 400
