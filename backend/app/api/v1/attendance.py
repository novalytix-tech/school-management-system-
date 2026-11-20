import uuid
from datetime import date, datetime, timedelta
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request
from app.extensions import db
from app.models import (
    StudentAttendance, StaffAttendance, StudentEnrollment, Student, Section, ClassLevel
)
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException
from app.utils.decorators import verify_teacher_class_access

attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")

# Statuses the StudentAttendance model and the attendance UI understand.
ALLOWED_STUDENT_STATUSES = {"PRESENT", "ABSENT", "LATE", "EXCUSED"}
MAX_REMARKS_LENGTH = 1000


def _bad_request(message: str):
    raise APIException(message, status_code=400, error_code="VALIDATION_ERROR")


def _parse_date(value, field: str = "date") -> date:
    if not isinstance(value, str):
        _bad_request(f"{field} must be a YYYY-MM-DD string")
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        _bad_request(f"{field} must be a valid YYYY-MM-DD date")


def _parse_uuid(value, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        _bad_request(f"{field} must be a UUID string")
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        _bad_request(f"{field} must be a valid UUID")


def _tenant_section(section_id: uuid.UUID, institution_id: uuid.UUID) -> Section:
    sec = (
        Section.query.join(ClassLevel, Section.class_level_id == ClassLevel.id)
        .filter(Section.id == section_id, ClassLevel.institution_id == institution_id)
        .first()
    )
    if not sec:
        raise APIException("Section not found", status_code=404, error_code="NOT_FOUND")
    return sec


def _is_admin() -> bool:
    roles = set((get_jwt() or {}).get("roles") or [])
    return bool(roles & {"super_admin", "school_admin"})


# -------------------------------------------------------------
# 1. Student Attendance
# -------------------------------------------------------------

@attendance_bp.route("/students", methods=["GET"])
def get_student_attendance():
    institution_id = get_current_tenant_id(required=True)
    date_str = request.args.get("date")
    section_id = request.args.get("section_id")

    query = (
        StudentAttendance.query
        .join(StudentEnrollment, StudentAttendance.enrollment_id == StudentEnrollment.id)
        .join(Student, StudentEnrollment.student_id == Student.id)
        .filter(Student.institution_id == institution_id)
    )
    if date_str:
        query = query.filter(StudentAttendance.attendance_date == _parse_date(date_str))

    if section_id:
        sec = _tenant_section(_parse_uuid(section_id, "section_id"), institution_id)
        query = query.filter(StudentEnrollment.section_id == sec.id)

    records = query.all()
    data = [
        {
            "id": str(r.id),
            "enrollment_id": str(r.enrollment_id),
            "student_id": str(r.enrollment.student_id) if r.enrollment else None,
            "student_name": r.enrollment.student.user.full_name if r.enrollment and r.enrollment.student and r.enrollment.student.user else "Student",
            "roll_number": r.enrollment.roll_number if r.enrollment else None,
            "attendance_date": r.attendance_date.isoformat(),
            "status": r.status,
            "remarks": r.remarks,
        }
        for r in records
    ]
    return jsonify({"success": True, "data": data}), 200


@attendance_bp.route("/students/batch", methods=["POST"])
def record_student_attendance_batch():
    """Bulk mark attendance for an entire classroom section."""
    institution_id = get_current_tenant_id(required=True)

    # The recorder is always the authenticated caller -- never a guessed user.
    verify_jwt_in_request()
    current_uid = get_jwt_identity()
    if not current_uid:
        raise APIException("Authentication required", status_code=401, error_code="UNAUTHORIZED")
    recorded_by_id = uuid.UUID(str(current_uid))

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        _bad_request("Request body must be a JSON object")

    items = payload.get("records")
    if items is None:
        items = payload.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        _bad_request("records must be a list")

    date_str = payload.get("date")
    att_date = _parse_date(date_str) if date_str is not None else date.today()
    # The server clock may be UTC while the school is ahead (IST is UTC+5:30), so
    # "today" for the school can be tomorrow on the server: allow one day of slack.
    if att_date > date.today() + timedelta(days=1):
        _bad_request("Attendance cannot be recorded for a future date")

    section = None
    if payload.get("section_id") is not None:
        section = _tenant_section(_parse_uuid(payload.get("section_id"), "section_id"), institution_id)

    # ---- Validate every row before writing anything ----
    resolved = []  # (enrollment, status, remarks)
    seen = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            _bad_request(f"records[{idx}] must be an object")

        enrollment_raw = item.get("enrollment_id") or item.get("enrollmentId")
        student_raw = item.get("student_id") or item.get("studentId")

        if enrollment_raw:
            e_uuid = _parse_uuid(enrollment_raw, f"records[{idx}].enrollment_id")
            enrollment = (
                StudentEnrollment.query
                .join(Student, StudentEnrollment.student_id == Student.id)
                .filter(StudentEnrollment.id == e_uuid, Student.institution_id == institution_id)
                .first()
            )
            if not enrollment:
                _bad_request(f"records[{idx}]: enrollment not found")
        elif student_raw:
            s_uuid = _parse_uuid(student_raw, f"records[{idx}].student_id")
            q = (
                StudentEnrollment.query
                .join(Student, StudentEnrollment.student_id == Student.id)
                .filter(
                    StudentEnrollment.student_id == s_uuid,
                    StudentEnrollment.status == "ACTIVE",
                    Student.institution_id == institution_id,
                )
            )
            if section is not None:
                q = q.filter(StudentEnrollment.section_id == section.id)
            enrollment = q.first()
            if not enrollment:
                _bad_request(f"records[{idx}]: no active enrollment found for student")
        else:
            _bad_request(f"records[{idx}]: enrollment_id or student_id is required")

        if section is not None and enrollment.section_id != section.id:
            _bad_request(f"records[{idx}]: enrollment does not belong to the given section")

        if enrollment.id in seen:
            _bad_request(f"records[{idx}]: duplicate entry for the same enrollment in one batch")
        seen.add(enrollment.id)

        status = item.get("status", "PRESENT")
        if not isinstance(status, str) or status.strip().upper() not in ALLOWED_STUDENT_STATUSES:
            _bad_request(
                f"records[{idx}].status must be one of {', '.join(sorted(ALLOWED_STUDENT_STATUSES))}"
            )
        status = status.strip().upper()

        remarks = item.get("remarks")
        if remarks is not None:
            if not isinstance(remarks, str):
                _bad_request(f"records[{idx}].remarks must be a string")
            if len(remarks) > MAX_REMARKS_LENGTH:
                _bad_request(f"records[{idx}].remarks must be at most {MAX_REMARKS_LENGTH} characters")

        resolved.append((enrollment, status, remarks))

    # ---- Teachers may only mark sections they teach / class-teach ----
    if not _is_admin():
        section_ids = {section.id} if section is not None else set()
        section_ids.update(e.section_id for e, _, _ in resolved)
        for sid in section_ids:
            verify_teacher_class_access(recorded_by_id, sid)

    results = []
    for enrollment, status, remarks in resolved:
        existing = StudentAttendance.query.filter_by(
            enrollment_id=enrollment.id,
            attendance_date=att_date
        ).first()

        if existing:
            existing.status = status
            existing.remarks = remarks
            existing.recorded_by = recorded_by_id
            results.append(str(existing.id))
        else:
            new_rec = StudentAttendance(
                enrollment_id=enrollment.id,
                attendance_date=att_date,
                status=status,
                remarks=remarks,
                recorded_by=recorded_by_id,
            )
            db.session.add(new_rec)
            db.session.flush()
            results.append(str(new_rec.id))

    db.session.commit()
    return jsonify({
        "success": True,
        "data": {"count": len(results), "date": att_date.isoformat()},
        "message": f"Successfully recorded attendance for {len(results)} students."
    }), 200


# -------------------------------------------------------------
# 2. Staff Attendance
# -------------------------------------------------------------

@attendance_bp.route("/staff", methods=["GET"])
def get_staff_attendance():
    institution_id = get_current_tenant_id(required=True)
    date_str = request.args.get("date")

    query = StaffAttendance.query.filter_by(institution_id=institution_id)

    if date_str:
        query = query.filter(StaffAttendance.attendance_date == _parse_date(date_str))

    records = query.all()
    data = [
        {
            "id": str(r.id),
            "staff_user_id": str(r.staff_user_id),
            "staff_name": f"{r.staff_user.first_name} {r.staff_user.last_name}".strip() if r.staff_user else None,
            "attendance_date": r.attendance_date.isoformat(),
            "check_in_time": r.check_in_time.strftime("%H:%M") if r.check_in_time else None,
            "check_out_time": r.check_out_time.strftime("%H:%M") if r.check_out_time else None,
            "status": r.status,
            "remarks": r.remarks,
        }
        for r in records
    ]
    return jsonify({"success": True, "data": data}), 200
