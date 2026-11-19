"""
Regression tests for the P0 "ghost login" bug.

Original defect: deleting a student only set ``students.status = 'WITHDRAWN'``.
The linked ``users`` row stayed active and was never filtered out of login or of
the lists/sync feed, so the deleted student could still authenticate and keep
appearing in the roster.

These tests reproduce that scenario and assert the fix:
  * a deleted student can no longer log in,
  * the DB record and its login account are both revoked,
  * active enrollments are deactivated,
  * the student disappears from the list and the sync/store hydration feed, and
  * a withdrawn student whose account was left active is still refused (defence
    in depth via the ACCOUNT_REVOKED guard).
"""
import uuid
from datetime import date, datetime, timezone

import pytest

from app.extensions import db
from app.models import (
    User, Student, Role, UserRole, Institution,
    AcademicYear, Section, StudentEnrollment,
)
from app.services.auth_service import AuthService
from app.utils.errors import APIException
from app.utils.security import hash_password


LOGIN_PASSWORD = "GhostTest#2026!"


def _make_login_student(with_enrollment: bool = True):
    """Create a fully active, verified student with a login account in the
    primary institution. Returns (student, user, enrollment_or_None)."""
    institution = Institution.query.filter_by(status="ACTIVE").first()
    assert institution is not None, "seed must provide an active institution"

    stamp = f"{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex[:6]}"
    email = f"ghost.{stamp}@student.stxaviers.edu.in"

    user = User(
        institution_id=institution.id,
        username=f"ghost_{stamp}",
        email=email,
        password_hash=hash_password(LOGIN_PASSWORD),
        first_name="Ghost",
        last_name="Student",
        is_active=True,
        is_verified=True,
    )
    db.session.add(user)
    db.session.flush()

    student_role = Role.query.filter_by(name="student").first()
    if student_role:
        db.session.add(UserRole(user_id=user.id, role_id=student_role.id))

    student = Student(
        institution_id=institution.id,
        user_id=user.id,
        admission_number=f"GHOST-{stamp}",
        admission_date=date.today(),
        date_of_birth=date(2011, 1, 1),
        gender="MALE",
        nationality="Indian",
        status="ACTIVE",
    )
    db.session.add(student)
    db.session.flush()

    enrollment = None
    if with_enrollment:
        section = Section.query.first()
        year = (
            AcademicYear.query.filter_by(institution_id=institution.id, is_current=True).first()
            or AcademicYear.query.filter_by(institution_id=institution.id).first()
        )
        if section and year:
            enrollment = StudentEnrollment(
                student_id=student.id,
                academic_year_id=year.id,
                section_id=section.id,
                roll_number="99",
                enrollment_date=date.today(),
                status="ACTIVE",
            )
            db.session.add(enrollment)

    db.session.commit()
    return student, user, enrollment


def test_deleted_student_cannot_login(client):
    """The core regression: after delete, the student's login is refused."""
    student, user, enrollment = _make_login_student()
    student_id = str(student.id)
    email = user.email

    # Baseline: before deletion the account can authenticate (this is exactly the
    # state that used to let a "deleted" student keep logging in).
    tokens = AuthService.authenticate_user(email, LOGIN_PASSWORD)
    assert tokens["access_token"], "a live student should be able to log in before deletion"

    # Act: delete via the real HTTP endpoint.
    res = client.delete(f"/api/v1/students/{student_id}")
    assert res.status_code == 200
    assert res.get_json()["success"] is True

    # The account must now be fully revoked.
    db.session.expire_all()
    refreshed_user = db.session.get(User, user.id)
    refreshed_student = db.session.get(Student, student.id)
    assert refreshed_student.is_deleted is True
    assert refreshed_student.status == "WITHDRAWN"
    assert refreshed_user.is_active is False
    assert refreshed_user.is_deleted is True
    if enrollment is not None:
        refreshed_enr = db.session.get(StudentEnrollment, enrollment.id)
        assert refreshed_enr.status == "INACTIVE"

    # Core assertion: login is refused for the deleted student.
    with pytest.raises(APIException):
        AuthService.authenticate_user(email, LOGIN_PASSWORD)


def test_deleted_student_disappears_from_list_and_sync(client):
    student, user, _ = _make_login_student(with_enrollment=False)
    student_id = str(student.id)
    admission_no = student.admission_number

    # Present before deletion.
    list_before = client.get("/api/v1/students?limit=100").get_json()["data"]["items"]
    assert any(s["id"] == student_id for s in list_before)

    assert client.delete(f"/api/v1/students/{student_id}").status_code == 200

    # Absent from the list afterwards.
    list_after = client.get("/api/v1/students?limit=100").get_json()["data"]["items"]
    assert all(s["id"] != student_id for s in list_after)

    # get_student resolves to 404.
    assert client.get(f"/api/v1/students/{student_id}").status_code == 404

    # Absent from the sync/store hydration feed (so a refresh can't resurrect it).
    store = client.get("/api/v1/sync/store").get_json()["data"]
    assert all(s.get("admissionNo") != admission_no for s in store["students"])


def test_withdrawn_student_with_active_account_is_refused(client):
    """Defence in depth: a withdrawn student whose users row was left active
    (e.g. legacy data) must still be refused via ACCOUNT_REVOKED."""
    student, user, _ = _make_login_student(with_enrollment=False)
    email = user.email

    # Simulate the legacy half-state: student withdrawn but account still live.
    student.status = "WITHDRAWN"
    student.is_deleted = False
    user.is_active = True
    user.is_deleted = False
    db.session.commit()

    with pytest.raises(APIException) as exc:
        AuthService.authenticate_user(email, LOGIN_PASSWORD)
    assert exc.value.error_code == "ACCOUNT_REVOKED"
