"""SCD Type 2 versioning for students.

The application records an immutable version row in ``student_history`` on every
student write path (create, edit, soft-delete). This is the documented choice of
*Flask service logic* over database triggers: it is testable with the existing
model-based test harness and captures every write that goes through the app's
service layer. (Trade-off: a raw SQL write straight to the ``students`` table
would bypass this — acceptable because all application writes are mediated here.)
"""
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import ProgrammingError, OperationalError

from app.extensions import db
from app.models import StudentHistory


def _active_enrollment(student):
    return next(
        (e for e in student.enrollments if e.status == "ACTIVE"),
        student.enrollments[0] if student.enrollments else None,
    )


def record_and_commit(student, operation: str, actor_id=None) -> bool:
    """Record a version and commit it, non-fatally.

    If the student_history table does not yet exist (e.g. the migration has not
    been applied to this environment), the version is skipped with a warning
    rather than breaking the student write that already committed. Returns True
    if a version was recorded.
    """
    try:
        record_student_version(student, operation, actor_id)
        db.session.commit()
        return True
    except (ProgrammingError, OperationalError):
        db.session.rollback()
        current_app.logger.warning(
            "student_history unavailable; skipped %s version (migration pending?)", operation
        )
        return False


def record_versions_bulk_and_commit(rows) -> bool:
    """Bulk-record initial versions and commit, non-fatally (see record_and_commit)."""
    try:
        record_student_versions_bulk(rows)
        db.session.commit()
        return True
    except (ProgrammingError, OperationalError):
        db.session.rollback()
        current_app.logger.warning("student_history unavailable; skipped bulk CREATE versions")
        return False


def record_student_version(student, operation: str, actor_id=None):
    """Close the student's current version (if any) and append a new current one.

    The caller is responsible for committing the surrounding transaction. ``student``
    must already reflect the new state (flush ORM changes before calling).
    """
    now = datetime.now(timezone.utc)

    # Close the existing current version(s).
    StudentHistory.query.filter_by(student_id=student.id, is_current=True).update(
        {"valid_to": now, "is_current": False}, synchronize_session=False
    )

    last_version = (
        db.session.query(func.max(StudentHistory.version_number))
        .filter(StudentHistory.student_id == student.id)
        .scalar()
    ) or 0

    u = getattr(student, "user", None)
    active = _active_enrollment(student)
    section = active.section if active else None

    db.session.add(StudentHistory(
        student_id=student.id,
        admission_number=student.admission_number,
        first_name=u.first_name if u else None,
        last_name=u.last_name if u else None,
        email=u.email if u else None,
        gender=student.gender,
        blood_group=student.blood_group,
        status=student.status,
        student_is_deleted=bool(student.is_deleted),
        section_id=active.section_id if active else None,
        class_name=(section.class_level.name if section and section.class_level else None),
        section_name=(section.name if section else None),
        roll_number=(active.roll_number if active else None),
        version_number=last_version + 1,
        valid_from=now,
        valid_to=None,
        is_current=True,
        operation=operation,
        changed_by=actor_id,
    ))


def record_student_versions_bulk(rows):
    """Insert initial CREATE versions for a batch of freshly imported students.

    ``rows`` is a list of dicts with keys: student_id, admission_number,
    first_name, last_name, email, gender, blood_group, status, section_id,
    class_name, section_name, roll_number. One INSERT covers the whole batch.
    """
    if not rows:
        return
    now = datetime.now(timezone.utc)
    history_rows = [{
        "id": __import__("uuid").uuid4(),
        "student_id": r["student_id"],
        "admission_number": r["admission_number"],
        "first_name": r.get("first_name"),
        "last_name": r.get("last_name"),
        "email": r.get("email"),
        "gender": r.get("gender"),
        "blood_group": r.get("blood_group"),
        "status": r.get("status", "ACTIVE"),
        "student_is_deleted": False,
        "section_id": r.get("section_id"),
        "class_name": r.get("class_name"),
        "section_name": r.get("section_name"),
        "roll_number": r.get("roll_number"),
        "version_number": 1,
        "valid_from": now,
        "valid_to": None,
        "is_current": True,
        "operation": "CREATE",
        "changed_by": r.get("changed_by"),
        "created_at": now,
    } for r in rows]
    from sqlalchemy import insert as sa_insert
    db.session.execute(sa_insert(StudentHistory), history_rows)
