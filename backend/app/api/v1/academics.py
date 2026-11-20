import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from flask import Blueprint, request, jsonify
from sqlalchemy import func
from app.extensions import db
from app.models import (
    AcademicYear, ClassLevel, Section, Subject, TimetableSlot
)
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException
from app.utils.cache_helpers import cached_response, invalidate_on_write

academics_bp = Blueprint("academics", __name__, url_prefix="/academics")

# Any successful write to this blueprint drops the cached academics reads for the
# tenant, so lists never go stale after a create/update/delete.
academics_bp.after_request(invalidate_on_write("academics"))


# -------------------------------------------------------------
# Input validation helpers (bad input -> 400, never a 500)
# -------------------------------------------------------------

def _bad_request(message: str):
    raise APIException(message, status_code=400, error_code="VALIDATION_ERROR")


def _conflict(message: str):
    raise APIException(message, status_code=409, error_code="DUPLICATE_RECORD")


def _json_object() -> dict:
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        _bad_request("Request body must be a JSON object")
    return payload


def _required_str(value, field: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        _bad_request(f"{field} is required and must be a non-empty string")
    value = value.strip()
    if len(value) > max_len:
        _bad_request(f"{field} must be at most {max_len} characters")
    return value


def _parse_date(value, field: str) -> date:
    if not isinstance(value, str):
        _bad_request(f"{field} must be a YYYY-MM-DD string")
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        _bad_request(f"{field} must be a valid YYYY-MM-DD date")


def _parse_int(value, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        _bad_request(f"{field} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        result = int(value.strip())
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    else:
        _bad_request(f"{field} must be an integer")
    if result < minimum or result > maximum:
        _bad_request(f"{field} must be between {minimum} and {maximum}")
    return result


def _parse_bool(value, field: str) -> bool:
    if isinstance(value, bool):
        return value
    _bad_request(f"{field} must be a boolean")


# -------------------------------------------------------------
# 1. Academic Sessions (Years) & Terms
# -------------------------------------------------------------

@academics_bp.route("/sessions", methods=["GET"])
@cached_response("academics", timeout=300)
def list_sessions():
    institution_id = get_current_tenant_id()
    query = AcademicYear.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    sessions = query.order_by(AcademicYear.start_date.desc()).all()
    data = [
        {
            "id": str(s.id),
            "name": s.name,
            "start_date": s.start_date.isoformat(),
            "end_date": s.end_date.isoformat(),
            "is_current": s.is_current,
            "terms": [
                {
                    "id": str(t.id),
                    "name": t.name,
                    "start_date": t.start_date.isoformat(),
                    "end_date": t.end_date.isoformat(),
                    "is_active": t.is_active,
                }
                for t in s.terms
            ]
        }
        for s in sessions
    ]
    return jsonify({"success": True, "data": data}), 200


@academics_bp.route("/sessions", methods=["POST"])
def create_session():
    institution_id = get_current_tenant_id(required=True)
    payload = _json_object()

    name = payload.get("name")
    start_date = payload.get("start_date") or payload.get("startDate")
    end_date = payload.get("end_date") or payload.get("endDate")
    is_current = _parse_bool(payload.get("is_current", False), "is_current")

    if not name or not start_date or not end_date:
        raise APIException("name, start_date, and end_date are required", status_code=400)

    name = _required_str(name, "name", 50)
    s_date = _parse_date(start_date, "start_date")
    e_date = _parse_date(end_date, "end_date")
    if e_date <= s_date:
        _bad_request("end_date must be after start_date")

    duplicate = AcademicYear.query.filter(
        AcademicYear.institution_id == institution_id,
        func.lower(AcademicYear.name) == name.lower(),
    ).first()
    if duplicate:
        _conflict(f"Academic session '{name}' already exists")

    if is_current:
        # Mark other sessions not current
        AcademicYear.query.filter_by(institution_id=institution_id).update({"is_current": False})

    session = AcademicYear(
        institution_id=institution_id,
        name=name,
        start_date=s_date,
        end_date=e_date,
        is_current=is_current,
    )
    db.session.add(session)
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {
            "id": str(session.id),
            "name": session.name,
            "start_date": session.start_date.isoformat(),
            "end_date": session.end_date.isoformat(),
            "is_current": session.is_current,
        },
        "message": "Academic session created successfully."
    }), 201


# -------------------------------------------------------------
# 2. Classes & Sections
# -------------------------------------------------------------

@academics_bp.route("/classes", methods=["GET"])
@cached_response("academics", timeout=300)
def list_classes():
    institution_id = get_current_tenant_id()
    query = ClassLevel.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    classes = query.order_by(ClassLevel.sequence_order.asc(), ClassLevel.name.asc()).all()
    data = []
    for c in classes:
        sections_data = []
        for sec in c.sections:
            teacher_name = None
            if sec.class_teacher:
                teacher_name = f"{sec.class_teacher.first_name} {sec.class_teacher.last_name}".strip()

            sections_data.append({
                "id": str(sec.id),
                "name": sec.name,
                "capacity": sec.capacity,
                "room_id": str(sec.room_id) if sec.room_id else None,
                "class_teacher_id": str(sec.class_teacher_id) if sec.class_teacher_id else None,
                "class_teacher_name": teacher_name,
                "subjects_count": len(sec.class_subjects),
            })

        data.append({
            "id": str(c.id),
            "name": c.name,
            "code": c.code,
            "sequence_order": c.sequence_order,
            "sections": sections_data,
        })

    return jsonify({"success": True, "data": data}), 200


@academics_bp.route("/classes", methods=["POST"])
def create_class():
    institution_id = get_current_tenant_id(required=True)
    payload = _json_object()

    if not payload.get("name"):
        raise APIException("Class name is required", status_code=400)
    name = _required_str(payload.get("name"), "name", 50)
    raw_code = payload.get("code")
    # Without an explicit code, derive one from the name (fits the 20-char column).
    code = (_required_str(raw_code, "code", 20) if raw_code else name[:20].strip()).upper()
    sequence = _parse_int(payload.get("sequence_order", 1), "sequence_order", 0, 10000)
    capacity = _parse_int(payload.get("capacity", 40), "capacity", 1, 1000)

    sections = payload.get("sections")
    if sections is None or sections == []:
        sections = ["A"]
    if not isinstance(sections, list):
        _bad_request("sections must be a list of section names")
    section_names = []
    for idx, sec_name in enumerate(sections):
        sec_name = _required_str(sec_name, f"sections[{idx}]", 10).upper()
        if sec_name in section_names:
            _bad_request(f"Duplicate section name '{sec_name}'")
        section_names.append(sec_name)

    if ClassLevel.query.filter(
        ClassLevel.institution_id == institution_id,
        func.lower(ClassLevel.name) == name.lower(),
    ).first():
        _conflict(f"Class '{name}' already exists")
    if ClassLevel.query.filter_by(institution_id=institution_id, code=code).first():
        _conflict(f"Class code '{code}' already exists")

    class_level = ClassLevel(
        institution_id=institution_id,
        name=name,
        code=code,
        sequence_order=sequence,
    )
    db.session.add(class_level)
    db.session.flush()

    # Create default section A if none specified
    for sec_name in section_names:
        db.session.add(Section(
            class_level_id=class_level.id,
            name=sec_name,
            capacity=capacity,
        ))

    db.session.commit()

    return jsonify({
        "success": True,
        "data": {
            "id": str(class_level.id),
            "name": class_level.name,
            "code": class_level.code,
            "sequence_order": class_level.sequence_order,
        },
        "message": "Class created successfully."
    }), 201


@academics_bp.route("/sections/<uuid:section_id>", methods=["GET"])
@cached_response("academics", timeout=300)
def get_section(section_id):
    institution_id = get_current_tenant_id(required=True)
    sec = (
        Section.query.join(ClassLevel, Section.class_level_id == ClassLevel.id)
        .filter(Section.id == section_id, ClassLevel.institution_id == institution_id)
        .first()
    )
    if not sec:
        raise APIException("Section not found", status_code=404)

    return jsonify({
        "success": True,
        "data": {
            "id": str(sec.id),
            "name": sec.name,
            "class_level_id": str(sec.class_level_id),
            "class_name": sec.class_level.name if sec.class_level else None,
            "capacity": sec.capacity,
            "subjects": [
                {
                    "class_subject_id": str(cs.id),
                    "subject_id": str(cs.subject_id),
                    "subject_name": cs.subject.name if cs.subject else None,
                    "subject_code": cs.subject.code if cs.subject else None,
                    "is_elective": cs.is_elective,
                }
                for cs in sec.class_subjects
            ]
        }
    }), 200


# -------------------------------------------------------------
# 3. Subjects
# -------------------------------------------------------------

@academics_bp.route("/subjects", methods=["GET"])
@cached_response("academics", timeout=300)
def list_subjects():
    institution_id = get_current_tenant_id()
    query = Subject.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    subjects = query.order_by(Subject.name.asc()).all()
    data = [
        {
            "id": str(s.id),
            "code": s.code,
            "name": s.name,
            "subject_type": s.subject_type,
            "credit_hours": float(s.credit_hours),
        }
        for s in subjects
    ]
    return jsonify({"success": True, "data": data}), 200


@academics_bp.route("/subjects", methods=["POST"])
def create_subject():
    institution_id = get_current_tenant_id(required=True)
    payload = _json_object()

    name = payload.get("name")
    code = payload.get("code")

    if not name or not code:
        raise APIException("Subject name and code are required", status_code=400)

    name = _required_str(name, "name", 100)
    code = _required_str(code, "code", 30).upper()
    subject_type = _required_str(payload.get("subject_type", "THEORY"), "subject_type", 30).upper()

    credit_hours = payload.get("credit_hours", 1.0)
    if isinstance(credit_hours, bool) or not isinstance(credit_hours, (int, float, str)):
        _bad_request("credit_hours must be a number")
    try:
        credit_hours = Decimal(str(credit_hours).strip())
    except (InvalidOperation, ValueError):
        _bad_request("credit_hours must be a number")
    if not credit_hours.is_finite() or credit_hours < 0 or credit_hours >= 1000:
        _bad_request("credit_hours must be between 0 and 999.9")

    # Codes are stored stripped + upper-cased; compare the same way (also
    # catches legacy rows stored with stray whitespace / lower case).
    exists = Subject.query.filter(
        Subject.institution_id == institution_id,
        func.upper(func.trim(Subject.code)) == code,
    ).first()
    if exists:
        _conflict(f"Subject code '{code}' already exists")

    subject = Subject(
        institution_id=institution_id,
        code=code,
        name=name,
        subject_type=subject_type,
        credit_hours=credit_hours,
    )
    db.session.add(subject)
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {
            "id": str(subject.id),
            "code": subject.code,
            "name": subject.name,
            "subject_type": subject.subject_type,
            "credit_hours": float(subject.credit_hours),
        },
        "message": "Subject created successfully."
    }), 201


# -------------------------------------------------------------
# 4. Timetable & Schedule Slots
# -------------------------------------------------------------

@academics_bp.route("/timetable", methods=["GET"])
@cached_response("academics", timeout=120)
def get_timetable():
    institution_id = get_current_tenant_id()
    section_id = request.args.get("section_id")

    query = TimetableSlot.query
    if institution_id:
        # Always scope to the caller's tenant, even when a section_id is given.
        query = query.join(Section, TimetableSlot.section_id == Section.id).join(
            ClassLevel, Section.class_level_id == ClassLevel.id
        ).filter(ClassLevel.institution_id == institution_id)
    if section_id:
        try:
            section_uuid = uuid.UUID(section_id.strip())
        except ValueError:
            _bad_request("section_id must be a valid UUID")
        query = query.filter(TimetableSlot.section_id == section_uuid)

    slots = query.all()
    data = []
    for s in slots:
        teacher_name = None
        if s.teacher:
            teacher_name = f"{s.teacher.first_name} {s.teacher.last_name}".strip()

        data.append({
            "id": str(s.id),
            "section_id": str(s.section_id),
            "section_name": s.section.name if s.section else None,
            "class_name": s.section.class_level.name if s.section and s.section.class_level else None,
            "period_id": str(s.period_id),
            "period_number": s.period.period_number if s.period else None,
            "start_time": s.period.start_time.strftime("%H:%M") if s.period else None,
            "end_time": s.period.end_time.strftime("%H:%M") if s.period else None,
            "day_of_week": s.day_of_week,
            "subject_id": str(s.subject_id),
            "subject_name": s.subject.name if s.subject else None,
            "teacher_id": str(s.teacher_id) if s.teacher_id else None,
            "teacher_name": teacher_name,
            "room_id": str(s.room_id) if s.room_id else None,
            "room_number": s.room.room_number if s.room else None,
        })

    return jsonify({"success": True, "data": data}), 200

