import os
import uuid
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import get_jwt, get_jwt_identity
from sqlalchemy import or_, and_, func, insert as sa_insert, delete as sa_delete
from sqlalchemy.exc import IntegrityError
from app.extensions import db
from app.models import (
    Student, StudentEnrollment, ClassLevel, Section, AcademicYear, User, StudentParent, Parent
)
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException
from app.utils.decorators import role_required

students_bp = Blueprint("students", __name__, url_prefix="/students")

# Lifecycle states a student record may carry (the frontend's "inactive" toggle
# maps to INACTIVE; WITHDRAWN is what delete/withdraw sets).
STUDENT_STATUSES = ("ACTIVE", "INACTIVE", "WITHDRAWN", "GRADUATED")

# Upper bound for ?page= so (page - 1) * limit can never overflow a BIGINT OFFSET.
MAX_PAGE = 1_000_000

# Column widths (mirrors app.models.student / app.models.user) for up-front checks.
_MAX_LEN = {
    "admission_number": 50, "gender": 20, "blood_group": 10, "nationality": 50,
    "religion": 50, "caste_category": 50, "emergency_contact_name": 100,
    "emergency_contact_phone": 50, "medical_notes": 5000, "status": 20,
    "first_name": 100, "last_name": 100, "email": 255, "phone": 50,
    "roll_number": 20, "username": 100,
}


def _bad_request(message, error_code="VALIDATION_ERROR", field=None):
    raise APIException(message, status_code=400, error_code=error_code,
                       details={"field": field} if field else None)


def _clean_str(value, field, required=False):
    """Coerce a JSON scalar to a stripped string (None / blank -> None).

    Numbers are accepted and stringified (e.g. an admission number sent as an
    int); objects, lists and booleans are rejected. Raises a 400 for a bad type,
    a missing required value, or a value longer than the column allows.
    """
    cleaned = None
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            _bad_request(f"'{field}' must be a string.", field=field)
        cleaned = str(value).strip() or None
    if cleaned is None:
        if required:
            _bad_request(f"'{field}' is required and cannot be empty.", "MISSING_FIELD", field)
        return None
    max_len = _MAX_LEN.get(field)
    if max_len and len(cleaned) > max_len:
        _bad_request(f"'{field}' must be at most {max_len} characters.", "FIELD_TOO_LONG", field)
    return cleaned


def _parse_date(value, field):
    """Parse a YYYY-MM-DD date (None / blank -> None); 400 on anything else."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        _bad_request(f"'{field}' must be a date string (YYYY-MM-DD).", "INVALID_DATE", field)
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        _bad_request(f"'{field}' must be a valid date (YYYY-MM-DD).", "INVALID_DATE", field)


def _json_object():
    """The request body as a dict; a JSON array/scalar body is a 400, not a 500."""
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        _bad_request("Request body must be a JSON object.", "INVALID_BODY")
    return payload


def _first_present(payload, *keys):
    """(present, value) for the first of ``keys`` present in the payload."""
    for k in keys:
        if k in payload:
            return True, payload[k]
    return False, None


def _page_and_limit():
    page = min(MAX_PAGE, max(1, request.args.get("page", 1, type=int)))
    limit = min(100, max(1, request.args.get("limit", 50, type=int)))
    return page, limit


def _revoke_access(student):
    """Withdrawal side effects shared with delete: block login, leave rosters."""
    if student.user:
        student.user.is_active = False
    for enrollment in student.enrollments:
        if enrollment.status == "ACTIVE":
            enrollment.status = "INACTIVE"


def _reinstate_access(student):
    """Undo _revoke_access/delete: re-enable login and the latest enrollment."""
    if student.user:
        student.user.is_active = True
        student.user.is_deleted = False
    if student.enrollments and not any(e.status == "ACTIVE" for e in student.enrollments):
        latest = max(
            student.enrollments,
            key=lambda e: (
                bool(e.academic_year and e.academic_year.is_current),
                e.enrollment_date or date.min,
                e.created_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        latest.status = "ACTIVE"


def _serialize_student(student: Student, full: bool = False):
    user_info = None
    if student.user:
        user_info = {
            "id": str(student.user.id),
            "email": student.user.email,
            "first_name": student.user.first_name,
            "last_name": student.user.last_name,
            "full_name": f"{student.user.first_name} {student.user.last_name}".strip(),
            "phone_number": student.user.phone,
        }

    # Latest active enrollment
    active_enrollment = next(
        (e for e in student.enrollments if e.status == "ACTIVE"),
        student.enrollments[0] if student.enrollments else None
    )

    class_info = None
    if active_enrollment and active_enrollment.section:
        sec = active_enrollment.section
        class_info = {
            "section_id": str(sec.id),
            "section_name": sec.name,
            "class_level_id": str(sec.class_level.id) if sec.class_level else None,
            "class_name": sec.class_level.name if sec.class_level else None,
            "roll_number": active_enrollment.roll_number,
            "academic_year_id": str(active_enrollment.academic_year_id),
        }

    # Parents
    parents_data = []
    for sp in student.student_parents:
        p = sp.parent
        if p:
            parents_data.append({
                "id": str(p.id),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "full_name": f"{p.first_name} {p.last_name}".strip(),
                "relationship": p.relationship_type,
                "is_primary": sp.is_primary_contact,
                "email": p.user.email if p.user else None,
                "phone": p.user.phone if p.user else None,
            })

    first_name = user_info["first_name"] if user_info else "Student"
    last_name = user_info["last_name"] if user_info else ""
    full_name = f"{first_name} {last_name}".strip()

    data = {
        "id": str(student.id),
        "institution_id": str(student.institution_id),
        "admission_number": student.admission_number,
        "admission_date": student.admission_date.isoformat() if student.admission_date else None,
        "date_of_birth": student.date_of_birth.isoformat() if student.date_of_birth else None,
        "gender": student.gender,
        "blood_group": student.blood_group,
        "nationality": student.nationality,
        "religion": student.religion,
        "caste_category": student.caste_category,
        "emergency_contact_name": student.emergency_contact_name,
        "emergency_contact_phone": student.emergency_contact_phone,
        "medical_notes": student.medical_notes,
        "status": student.status,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "email": user_info["email"] if user_info else None,
        "phone": user_info["phone_number"] if user_info else None,
        "class_info": class_info,
        "parents": parents_data,
        "created_at": student.created_at.isoformat() if student.created_at else None,
    }

    if full:
        data["enrollment_history"] = [
            {
                "id": str(e.id),
                "academic_year_id": str(e.academic_year_id),
                "academic_year_name": e.academic_year.name if e.academic_year else None,
                "section_id": str(e.section_id),
                "section_name": e.section.name if e.section else None,
                "class_name": e.section.class_level.name if e.section and e.section.class_level else None,
                "roll_number": e.roll_number,
                "status": e.status,
                "enrollment_date": e.enrollment_date.isoformat() if e.enrollment_date else None,
            }
            for e in student.enrollments
        ]

    return data


@students_bp.route("", methods=["GET"])
def list_students():
    """List students with search, section filter, pagination, and sorting."""
    if not current_app.config.get("DATABASE_ENABLED", False):
        from app.api.v1.sync import _get_fallback_store
        fb = _get_fallback_store()
        return jsonify({
            "success": True,
            "data": fb.get("students", []),
            "total": len(fb.get("students", [])),
            "page": 1,
            "per_page": 50,
        }), 200

    institution_id = get_current_tenant_id()
    # Never surface soft-deleted students: once removed they must not reappear
    # in any list or after a store re-hydrate.
    query = Student.query.filter(Student.is_deleted.is_(False))

    if institution_id:
        query = query.filter(Student.institution_id == institution_id)

    # Search filter
    search = request.args.get("search", "").strip()
    if search:
        search_filter = or_(
            Student.admission_number.ilike(f"%{search}%"),
            Student.emergency_contact_name.ilike(f"%{search}%"),
            Student.emergency_contact_phone.ilike(f"%{search}%"),
            Student.user.has(User.first_name.ilike(f"%{search}%")),
            Student.user.has(User.last_name.ilike(f"%{search}%")),
            Student.user.has(User.email.ilike(f"%{search}%")),
        )
        query = query.filter(search_filter)

    # Status filter. By default withdrawn students are hidden; callers can still
    # request them explicitly with ?status=WITHDRAWN.
    status = request.args.get("status")
    if status:
        query = query.filter(Student.status == status.upper())
    else:
        query = query.filter(Student.status != "WITHDRAWN")

    # Section / Class filter
    section_id = request.args.get("section_id")
    if section_id:
        try:
            sec_uuid = uuid.UUID(section_id)
        except (ValueError, TypeError):
            raise APIException("Invalid section_id", status_code=400, error_code="INVALID_SECTION_ID")
        query = query.filter(Student.enrollments.any(
            and_(StudentEnrollment.section_id == sec_uuid, StudentEnrollment.status == "ACTIVE")
        ))

    # Pagination (page is capped so the OFFSET can never overflow)
    page, limit = _page_and_limit()
    total = query.count()
    students = query.order_by(Student.admission_number.asc()).offset((page - 1) * limit).limit(limit).all()

    return jsonify({
        "success": True,
        "data": {
            "items": [_serialize_student(s) for s in students],
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit,
        }
    }), 200


@students_bp.route("/<uuid:student_id>", methods=["GET"])
def get_student(student_id):
    """Retrieve detailed student profile."""
    institution_id = get_current_tenant_id()
    query = Student.query.filter(Student.id == student_id, Student.is_deleted.is_(False))
    if institution_id:
        query = query.filter(Student.institution_id == institution_id)

    student = query.first()
    if not student:
        raise APIException("Student record not found", status_code=404, error_code="STUDENT_NOT_FOUND")

    # Staff may open any profile in their school; a parent only their own wards
    # and a student only their own record. Others get the same 404 as an
    # unknown id so record ids can't be probed.
    claims = get_jwt()
    roles = set(claims.get("roles", []))
    if not roles.intersection({"super_admin", "school_admin", "teacher"}):
        user_id = uuid.UUID(get_jwt_identity())
        allowed = False
        if "student" in roles and student.user_id == user_id:
            allowed = True
        if not allowed and "parent" in roles:
            allowed = (
                StudentParent.query.join(Parent, StudentParent.parent_id == Parent.id)
                .filter(StudentParent.student_id == student.id, Parent.user_id == user_id)
                .first()
                is not None
            )
        if not allowed:
            raise APIException("Student record not found", status_code=404, error_code="STUDENT_NOT_FOUND")

    return jsonify({
        "success": True,
        "data": _serialize_student(student, full=True)
    }), 200


@students_bp.route("/<uuid:student_id>/history", methods=["GET"])
def get_student_history(student_id):
    """Return the SCD2 version timeline for a student (newest version first).

    Soft-deleted/withdrawn students are included here on purpose, so their change
    log stays viewable after removal. Default lists elsewhere show only the live,
    is_current record.
    """
    from app.models import StudentHistory
    from sqlalchemy.exc import ProgrammingError, OperationalError

    institution_id = get_current_tenant_id()
    sq = Student.query.filter(Student.id == student_id)
    if institution_id:
        sq = sq.filter(Student.institution_id == institution_id)
    if not sq.first():
        raise APIException("Student record not found", status_code=404, error_code="STUDENT_NOT_FOUND")

    try:
        versions = (
            StudentHistory.query.filter_by(student_id=student_id)
            .order_by(StudentHistory.version_number.desc())
            .all()
        )
    except (ProgrammingError, OperationalError):
        # History table not yet migrated in this environment; degrade gracefully.
        db.session.rollback()
        versions = []
    items = [{
        "id": str(v.id),
        "version_number": v.version_number,
        "operation": v.operation,
        "is_current": v.is_current,
        "valid_from": v.valid_from.isoformat() if v.valid_from else None,
        "valid_to": v.valid_to.isoformat() if v.valid_to else None,
        "admission_number": v.admission_number,
        "full_name": f"{v.first_name or ''} {v.last_name or ''}".strip(),
        "first_name": v.first_name,
        "last_name": v.last_name,
        "email": v.email,
        "gender": v.gender,
        "blood_group": v.blood_group,
        "status": v.status,
        "is_deleted": v.student_is_deleted,
        "class_name": v.class_name,
        "section_name": v.section_name,
        "section_id": str(v.section_id) if v.section_id else None,
        "roll_number": v.roll_number,
        "changed_by": str(v.changed_by) if v.changed_by else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    } for v in versions]

    return jsonify({
        "success": True,
        "data": {
            "student_id": str(student_id),
            "total": len(items),
            "versions": items,
        }
    }), 200


def _import_lookups(institution_id):
    """Sections map, student role id and active academic year.

    Loaded fresh per request (three cheap queries). A per-worker module cache
    used to live here, but it went stale — a newly created section or class
    teacher was invisible to imports for up to a minute on that worker.
    """
    from app.models.user import Role

    sections_map = {}
    sections_without_teacher = set()
    section_names = {}  # section_id -> (class_name, section_name) for display/history
    for sec_id, sec_name, cls_name, class_teacher_id in (
        db.session.query(Section.id, Section.name, ClassLevel.name, Section.class_teacher_id)
        .join(ClassLevel, Section.class_level_id == ClassLevel.id)
        .filter(ClassLevel.institution_id == institution_id)
        .all()
    ):
        sections_map[((cls_name or "").strip().lower(), (sec_name or "").strip().lower())] = sec_id
        section_names[sec_id] = (cls_name, sec_name)
        if class_teacher_id is None:
            sections_without_teacher.add(sec_id)

    role = Role.query.filter_by(name="student").first()
    year = (
        AcademicYear.query.filter_by(institution_id=institution_id, is_current=True).first()
        or AcademicYear.query.filter_by(institution_id=institution_id).first()
    )

    return (
        sections_map,
        role.id if role else None,
        year.id if year else None,
        year.name if year else "2026-2027",
        sections_without_teacher,
        section_names,
    )


def _gen_temp_password() -> str:
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    nums = "23456789"
    special = "!@#$"
    return (
        "".join(secrets.choice(chars) for _ in range(4))
        + "#"
        + "".join(secrets.choice(nums) for _ in range(3))
        + secrets.choice(special)
    )


@students_bp.route("/bulk-import", methods=["POST"])
def bulk_import_students():
    """
    Bulk-create students from imported rows.

    Performance: every lookup is pre-loaded in a fixed number of queries and all
    uniqueness checks run in memory, primary keys are generated client-side so no
    per-row FLUSH is needed, password hashing runs on a thread pool, and the whole
    batch is written in a single COMMIT. Cost is O(1) round-trips, not O(rows).
    """
    if not current_app.config.get("DATABASE_ENABLED", False):
        raise APIException("Database is not enabled.", status_code=503, error_code="DATABASE_UNAVAILABLE")

    from app.utils.security import hash_temp_password
    from app.models.user import UserRole

    institution_id = get_current_tenant_id(required=True)
    body = _json_object()
    rows = body.get("students") or body.get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise APIException("No student rows provided.", status_code=400, error_code="NO_ROWS")

    # Keeps CSV row numbers correct across batches; a malformed value just means 0.
    try:
        row_offset = max(0, int(body.get("rowOffset") or 0))
    except (TypeError, ValueError, OverflowError):
        row_offset = 0

    # ---------- Bulk pre-load: fixed number of queries, never per row ----------
    existing_adm = {
        (a or "").strip().lower()
        for (a,) in db.session.query(Student.admission_number).filter(
            Student.institution_id == institution_id
        ).all()
    }
    existing_usernames, existing_emails = set(), set()
    for uname, mail in db.session.query(User.username, User.email).all():
        if uname:
            existing_usernames.add(uname.strip().lower())
        if mail:
            existing_emails.add(mail.strip().lower())

    # Sections / role / academic year change rarely, so they are cached briefly and
    # cost zero round-trips on every batch after the first.
    sections_map, student_role_id, year_id, year_name, sections_without_teacher, section_names = _import_lookups(institution_id)
    valid_section_ids = set(sections_map.values())
    if not year_id:
        raise APIException(
            "No academic year is configured; cannot import students.",
            status_code=400, error_code="NO_ACADEMIC_YEAR",
        )

    # ---------- Validate + prepare entirely in memory ----------
    prepared, errors, import_warnings = [], [], set()
    def _s(value):
        """Row cell as a stripped string; numbers are stringified, objects dropped."""
        if value is None or isinstance(value, (dict, list, bool)):
            return ""
        return str(value).strip()

    for i, row in enumerate(rows):
        csv_row = row_offset + i + 2  # +2: header line plus 1-based numbering

        if not isinstance(row, dict):
            errors.append({"row": csv_row, "name": "", "reason": "Row is not a valid record"})
            continue

        full_name = _s(row.get("fullName") or row.get("full_name") or row.get("name"))
        if not full_name:
            errors.append({"row": csv_row, "name": "", "reason": "Name is missing"})
            continue

        # Integrity rule (Task 2): resolve the Class + Section up front. A row that
        # can't be mapped to an existing section is a surfaced error (skipped),
        # never a silent import of a student with no enrollment.
        # A caller (e.g. the Classes page) may pass an explicit section id, which
        # is used directly; otherwise we match on class + section names.
        class_name_val = _s(row.get("className") or row.get("class"))
        section_val = _s(row.get("section"))
        resolved_section_id = None
        explicit_section = row.get("section_id") or row.get("sectionId") or row.get("classId")
        if explicit_section:
            try:
                candidate = uuid.UUID(str(explicit_section))
                if candidate in valid_section_ids:
                    resolved_section_id = candidate
            except (ValueError, TypeError):
                resolved_section_id = None
        if resolved_section_id is None:
            resolved_section_id = sections_map.get((class_name_val.lower(), section_val.lower()))
        if not resolved_section_id:
            errors.append({
                "row": csv_row,
                "name": full_name,
                "reason": f"Class/Section '{class_name_val or '—'} / {section_val or '—'}' not found. "
                          "Assign a valid existing class and section.",
            })
            continue
        # Fill display names from the resolved section if the row didn't carry them.
        if resolved_section_id in section_names:
            rn_cls, rn_sec = section_names[resolved_section_id]
            class_name_val = class_name_val or (rn_cls or "")
            section_val = section_val or (rn_sec or "")
        if resolved_section_id in sections_without_teacher:
            sec_label = f"{class_name_val} {section_val}".strip() or "The assigned section"
            import_warnings.add(f"{sec_label} has no class teacher assigned.")

        parts = full_name.split(" ")
        first_name = _s(row.get("firstName")) or parts[0] or full_name
        last_name = _s(row.get("lastName")) or (" ".join(parts[1:]).strip() if len(parts) > 1 else first_name)

        admission_no = _s(row.get("admissionNo") or row.get("admission_number"))
        if not admission_no:
            admission_no = "ADM-" + secrets.token_hex(3).upper()

        gender_val = (_s(row.get("gender")) or "OTHER").upper()
        blood_group_val = _s(row.get("bloodGroup") or row.get("blood_group")) or None
        phone_val = _s(row.get("parentPhone") or row.get("phone")) or None
        roll_val = _s(row.get("rollNo") or row.get("roll_number")) or None
        too_long = next((
            label for label, val, key in (
                ("Admission number", admission_no, "admission_number"),
                ("First name", first_name, "first_name"),
                ("Last name", last_name, "last_name"),
                ("Gender", gender_val, "gender"),
                ("Blood group", blood_group_val, "blood_group"),
                ("Phone", phone_val, "phone"),
                ("Roll number", roll_val, "roll_number"),
            ) if val and len(val) > _MAX_LEN[key]
        ), None)
        if too_long:
            errors.append({"row": csv_row, "name": full_name, "reason": f"{too_long} is too long"})
            continue

        if admission_no.lower() in existing_adm:
            errors.append({
                "row": csv_row,
                "name": full_name,
                "reason": "Duplicate admission number '" + admission_no + "'",
            })
            continue
        existing_adm.add(admission_no.lower())

        username = (_s(row.get("loginId")) or admission_no)[:90]
        while username.lower() in existing_usernames:
            username = admission_no[:40] + "-" + secrets.token_hex(2)
        existing_usernames.add(username.lower())

        email = _s(row.get("parentEmail") or row.get("email")).lower()
        if not email or len(email) > _MAX_LEN["email"] or email in existing_emails:
            email = username.lower() + "@student.local"
            while email in existing_emails:
                email = username.lower() + "-" + secrets.token_hex(2) + "@student.local"
        existing_emails.add(email)

        dob = _s(row.get("dob") or row.get("date_of_birth"))
        try:
            dob_val = datetime.strptime(dob[:10], "%Y-%m-%d").date() if dob else date(2012, 1, 1)
        except ValueError:
            dob_val = date(2012, 1, 1)

        prepared.append({
            "full_name": full_name, "first_name": first_name, "last_name": last_name,
            "admission_no": admission_no, "username": username, "email": email,
            "dob": dob_val,
            "gender": gender_val,
            "blood_group": blood_group_val,
            "phone": phone_val,
            "class_name": class_name_val,
            "section": section_val,
            "section_id": resolved_section_id,
            "roll_no": roll_val,
            "parent_name": _s(row.get("parentName")) or "Guardian",
            "parent_email": _s(row.get("parentEmail")),
            "address": _s(row.get("address")),
        })

    # ---------- Hash every temporary password in parallel ----------
    passwords = [_gen_temp_password() for _ in prepared]
    if passwords:
        workers = min(8, (os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            hashes = list(pool.map(hash_temp_password, passwords))
    else:
        hashes = []

    # ---------- Build plain dicts, then write each table in ONE multi-row INSERT ----------
    today = date.today()
    now = datetime.now(timezone.utc)
    base = {"is_deleted": False, "created_at": now, "updated_at": now}
    user_rows, role_rows, student_rows, enroll_rows = [], [], [], []
    created, credentials, history_inputs = [], [], []

    for p, plain_pw, pw_hash in zip(prepared, passwords, hashes):
        user_id = uuid.uuid4()
        student_id = uuid.uuid4()

        user_rows.append({
            **base,
            "id": user_id,
            "institution_id": institution_id,
            "username": p["username"],
            "email": p["email"],
            "password_hash": pw_hash,
            "first_name": p["first_name"],
            "last_name": p["last_name"],
            "phone": p["phone"],
            "is_active": True,
            "is_verified": True,
            "must_change_password": True,
        })
        if student_role_id:
            role_rows.append({
                **base,
                "id": uuid.uuid4(),
                "user_id": user_id,
                "role_id": student_role_id,
            })

        student_rows.append({
            **base,
            "id": student_id,
            "institution_id": institution_id,
            "user_id": user_id,
            "admission_number": p["admission_no"],
            "admission_date": today,
            "date_of_birth": p["dob"],
            "gender": str(p["gender"]).upper(),
            "blood_group": p["blood_group"],
            "nationality": "Indian",
            "caste_category": "GENERAL",
            "status": "ACTIVE",
        })

        # Every prepared row has a validated section, so enrollment is unconditional.
        enroll_rows.append({
            **base,
            "id": uuid.uuid4(),
            "student_id": student_id,
            "academic_year_id": year_id,
            "section_id": p["section_id"],
            "roll_number": p["roll_no"],
            "enrollment_date": today,
            "status": "ACTIVE",
        })

        history_inputs.append({
            "student_id": student_id,
            "admission_number": p["admission_no"],
            "first_name": p["first_name"],
            "last_name": p["last_name"],
            "email": p["email"],
            "gender": str(p["gender"]).upper(),
            "blood_group": p["blood_group"],
            "status": "ACTIVE",
            "section_id": p["section_id"],
            "class_name": p["class_name"],
            "section_name": p["section"],
            "roll_number": str(p["roll_no"]) if p["roll_no"] else None,
        })

        created.append({
            "id": str(student_id),
            "admissionNo": p["admission_no"],
            "firstName": p["first_name"],
            "lastName": p["last_name"],
            "fullName": p["full_name"],
            "gender": p["gender"],
            "dob": p["dob"].isoformat(),
            "bloodGroup": p["blood_group"] or "",
            "classId": "",
            "className": p["class_name"],
            "section": p["section"] or "A",
            "rollNo": str(p["roll_no"] or ""),
            "academicYear": year_name,
            "admissionDate": today.isoformat(),
            "parentName": p["parent_name"],
            "parentPhone": p["phone"] or "",
            "parentEmail": p["parent_email"],
            "address": p["address"],
            "status": "active",
        })
        credentials.append({
            "name": p["full_name"],
            "admissionNo": p["admission_no"],
            "className": p["class_name"],
            "section": p["section"],
            "loginId": p["username"],
            "temporaryPassword": plain_pw,
        })

    # ---------- One multi-row INSERT per table, then a single COMMIT ----------
    try:
        if user_rows:
            db.session.execute(sa_insert(User), user_rows)
        if role_rows:
            db.session.execute(sa_insert(UserRole), role_rows)
        if student_rows:
            db.session.execute(sa_insert(Student), student_rows)
        if enroll_rows:
            db.session.execute(sa_insert(StudentEnrollment), enroll_rows)
        db.session.commit()
    except IntegrityError:
        # A concurrent create/import claimed one of these admission numbers,
        # usernames or emails between our pre-load and the INSERT.
        db.session.rollback()
        raise APIException(
            "Another request created a conflicting student or login concurrently; "
            "nothing was imported. Please retry.",
            status_code=409,
            error_code="DUPLICATE_RECORD",
        )
    except Exception:
        db.session.rollback()
        # Details (SQL, column names) go to the server log, never to the client.
        current_app.logger.exception("Bulk student import rejected by the database")
        raise APIException(
            "The database rejected this import batch; nothing was imported. "
            "Check the rows for invalid or over-long values and retry.",
            status_code=400,
            error_code="IMPORT_DB_ERROR",
        )

    # SCD2: seed an initial CREATE version per imported student, in a separate
    # non-fatal transaction so a pending migration can't fail the committed import.
    from app.services.student_history_service import record_versions_bulk_and_commit
    record_versions_bulk_and_commit(history_inputs)

    # Realtime: notify other clients that a batch of students was imported.
    if created:
        from app.services.realtime_service import emit_entity_change
        emit_entity_change(institution_id, "student", "imported", {"count": len(created)})

    return jsonify({
        "success": True,
        "data": {
            "created": created,
            "credentials": credentials,
            "imported": len(created),
            "skipped": len(errors),
            "errors": errors,
            "warnings": sorted(import_warnings),
        },
        "message": str(len(created)) + " student(s) imported, " + str(len(errors)) + " skipped.",
    }), 201



@students_bp.route("", methods=["POST"])
def create_student():
    """Create a new student with enrollment and optional user account."""
    if not current_app.config.get("DATABASE_ENABLED", False):
        payload = request.get_json() or {}
        adm_no = payload.get("admission_number") or payload.get("admissionNo") or f"ADM-2026-{int(datetime.now().timestamp())}"
        first_name = payload.get("first_name") or payload.get("firstName") or "Student"
        last_name = payload.get("last_name") or payload.get("lastName") or ""
        return jsonify({
            "success": True,
            "data": {
                "id": str(uuid.uuid4()),
                "admission_number": adm_no,
                "first_name": first_name,
                "last_name": last_name,
            },
            "message": "Student created in local mock runtime (Database disconnected)."
        }), 201

    institution_id = get_current_tenant_id(required=True)
    payload = _json_object()

    admission_no = _clean_str(
        payload.get("admission_number") or payload.get("admissionNo"), "admission_number"
    )
    if not admission_no:
        raise APIException("Admission number is required", status_code=400, error_code="MISSING_ADMISSION_NO")

    # Validate every scalar up front (type, date format, column width) so bad
    # input is a 400 and nothing is written.
    dob_val = _parse_date(payload.get("date_of_birth") or payload.get("dob"), "date_of_birth") or date(2010, 1, 1)
    adm_date_val = _parse_date(payload.get("admission_date"), "admission_date") or date.today()
    gender = (_clean_str(payload.get("gender"), "gender") or "OTHER").upper()
    blood_group = _clean_str(payload.get("blood_group") or payload.get("bloodGroup"), "blood_group")
    nationality = _clean_str(payload.get("nationality"), "nationality") or "Indian"
    religion = _clean_str(payload.get("religion"), "religion")
    caste_category = _clean_str(payload.get("caste_category"), "caste_category") or "GENERAL"
    emergency_name = _clean_str(
        payload.get("emergency_contact_name") or payload.get("emergencyContact"), "emergency_contact_name"
    )
    emergency_phone = _clean_str(
        payload.get("emergency_contact_phone") or payload.get("emergencyPhone"), "emergency_contact_phone"
    )
    medical_notes = _clean_str(payload.get("medical_notes"), "medical_notes")
    roll_number = _clean_str(payload.get("roll_number") or payload.get("rollNo"), "roll_number")
    first_name = _clean_str(payload.get("first_name") or payload.get("firstName"), "first_name") or "Student"
    last_name = _clean_str(payload.get("last_name") or payload.get("lastName"), "last_name") or ""
    phone = _clean_str(payload.get("phone") or payload.get("phone_number"), "phone")
    email = _clean_str(payload.get("email"), "email")
    if email:
        email = email.lower()
        if "@" not in email:
            _bad_request("'email' must be a valid email address.", "INVALID_EMAIL", "email")
        # Never attach an existing login (a teacher's, parent's, or another
        # school's user) to a new student: deleting the student would then
        # deactivate that unrelated account.
        if User.query.filter(func.lower(User.email) == email).first():
            raise APIException(
                f"The email '{email}' is already used by another account.",
                status_code=409, error_code="EMAIL_IN_USE",
            )

    # Check duplicate admission number (case-insensitive, matching bulk import;
    # soft-deleted rows count too because the DB unique constraint covers them).
    exists = Student.query.filter(
        Student.institution_id == institution_id,
        func.lower(Student.admission_number) == admission_no.lower(),
    ).first()
    if exists:
        raise APIException(f"Admission number '{admission_no}' already exists in this institution", status_code=409, error_code="DUPLICATE_ADMISSION_NO")

    # --- Integrity rule (Task 2): every student must be assigned to a valid
    # Class + Section. Validate BEFORE creating any user/student rows so a
    # rejected request writes nothing. ---
    section_ref = payload.get("section_id") or payload.get("sectionId") or payload.get("classId")
    if not section_ref:
        raise APIException(
            "A student must be assigned to a class and section.",
            status_code=400, error_code="SECTION_REQUIRED",
        )
    try:
        section_uuid = uuid.UUID(str(section_ref))
    except (ValueError, TypeError):
        raise APIException(
            "Invalid class/section identifier.",
            status_code=400, error_code="INVALID_SECTION_ID",
        )
    section_obj = Section.query.filter_by(id=section_uuid).first()
    if not section_obj or not section_obj.class_level or section_obj.class_level.institution_id != institution_id:
        raise APIException(
            "The selected class/section was not found for this institution.",
            status_code=400, error_code="SECTION_NOT_FOUND",
        )
    active_year = (
        AcademicYear.query.filter_by(institution_id=institution_id, is_current=True).first()
        or AcademicYear.query.filter_by(institution_id=institution_id).first()
    )
    if not active_year:
        raise APIException(
            "No academic year is configured; cannot enroll the student.",
            status_code=400, error_code="NO_ACADEMIC_YEAR",
        )
    warnings = []
    if not section_obj.class_teacher_id:
        warnings.append(
            f"Section '{section_obj.name}' has no class teacher assigned. "
            "Assign a class teacher to complete the record."
        )

    try:
        user_obj = None
        if email:
            from app.utils.security import hash_password
            from app.models.user import Role, UserRole
            username = email.split('@')[0][:80] or "student"
            # Ensure unique username
            if User.query.filter(func.lower(User.username) == username.lower()).first():
                username = f"{username}_{secrets.token_hex(4)}"

            user_obj = User(
                institution_id=institution_id,
                username=username,
                email=email,
                password_hash=hash_password("SchoolERP@Demo2026!"),
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                is_active=True,
            )
            db.session.add(user_obj)
            db.session.flush()

            # Assign student role
            student_role = Role.query.filter_by(name="student").first()
            if student_role:
                db.session.add(UserRole(user_id=user_obj.id, role_id=student_role.id))

        student = Student(
            institution_id=institution_id,
            user_id=user_obj.id if user_obj else None,
            admission_number=admission_no,
            admission_date=adm_date_val,
            date_of_birth=dob_val,
            gender=gender,
            blood_group=blood_group,
            nationality=nationality,
            religion=religion,
            caste_category=caste_category,
            emergency_contact_name=emergency_name,
            emergency_contact_phone=emergency_phone,
            medical_notes=medical_notes,
            status="ACTIVE",
        )
        db.session.add(student)
        db.session.flush()

        # Enrollment — section and academic year were validated above, so every
        # created student is guaranteed an active Class + Section (no orphans).
        enrollment = StudentEnrollment(
            student_id=student.id,
            academic_year_id=active_year.id,
            section_id=section_uuid,
            roll_number=roll_number,
            enrollment_date=date.today(),
            status="ACTIVE",
        )
        db.session.add(enrollment)

        db.session.commit()
    except IntegrityError:
        # Lost a race with a concurrent create for the same admission number,
        # username or email: roll back everything (no half-created user/student).
        db.session.rollback()
        raise APIException(
            f"Admission number '{admission_no}' or the login email was just taken by another request.",
            status_code=409, error_code="DUPLICATE_RECORD",
        )
    except Exception:
        db.session.rollback()
        raise

    # SCD2: record the initial version of the student (expire_on_commit means the
    # student now reflects its persisted state, including the enrollment).
    from app.services.student_history_service import record_and_commit
    record_and_commit(student, "CREATE")

    # Realtime: tell other clients a student was added so their lists update live.
    from app.services.realtime_service import emit_entity_change
    emit_entity_change(institution_id, "student", "created",
                       {"id": str(student.id), "admission_number": student.admission_number})

    resp = {
        "success": True,
        "data": _serialize_student(student),
        "message": f"Student {student.admission_number} created successfully.",
    }
    if warnings:
        resp["warnings"] = warnings
    return jsonify(resp), 201


@students_bp.route("/<uuid:student_id>", methods=["PUT", "PATCH"])
def update_student(student_id):
    """Update student profile attributes."""
    if not current_app.config.get("DATABASE_ENABLED", False):
        return jsonify({
            "success": True,
            "message": "Student updated in local mock runtime (Database disconnected)."
        }), 200

    institution_id = get_current_tenant_id()
    # A soft-deleted student is gone until explicitly restored (POST .../restore);
    # editing it must not silently half-resurrect the record.
    query = Student.query.filter(Student.id == student_id, Student.is_deleted.is_(False))
    if institution_id:
        query = query.filter(Student.institution_id == institution_id)

    student = query.first()
    if not student:
        raise APIException("Student record not found", status_code=404, error_code="STUDENT_NOT_FOUND")

    payload = _json_object()

    # Validate everything first, then apply, so a 400 never leaves partial edits.
    student_changes, user_changes = {}, {}
    if "gender" in payload:
        student_changes["gender"] = _clean_str(payload["gender"], "gender", required=True).upper()
    present, value = _first_present(payload, "blood_group", "bloodGroup")
    if present:
        student_changes["blood_group"] = _clean_str(value, "blood_group")
    if "nationality" in payload:
        student_changes["nationality"] = _clean_str(payload["nationality"], "nationality", required=True)
    for field in ("emergency_contact_name", "emergency_contact_phone", "medical_notes"):
        if field in payload:
            student_changes[field] = _clean_str(payload[field], field)
    new_status = None
    if "status" in payload:
        new_status = _clean_str(payload["status"], "status", required=True).upper()
        if new_status not in STUDENT_STATUSES:
            raise APIException(
                f"Invalid status '{new_status}'. Allowed: {', '.join(STUDENT_STATUSES)}.",
                status_code=400, error_code="INVALID_STATUS", details={"field": "status"},
            )

    present, value = _first_present(payload, "first_name", "firstName")
    if present:
        user_changes["first_name"] = _clean_str(value, "first_name", required=True)
    present, value = _first_present(payload, "last_name", "lastName")
    if present:
        user_changes["last_name"] = _clean_str(value, "last_name") or ""
    present, value = _first_present(payload, "phone", "phone_number")
    if present:
        user_changes["phone"] = _clean_str(value, "phone")

    try:
        for attr, val in student_changes.items():
            setattr(student, attr, val)
        if student.user:
            for attr, val in user_changes.items():
                setattr(student.user, attr, val)

        # Status transitions carry the same access side effects as delete/restore:
        # withdrawing revokes the login and roster place; re-activating a
        # withdrawn student gives both back.
        if new_status and new_status != student.status:
            previous = student.status
            student.status = new_status
            if new_status == "WITHDRAWN":
                _revoke_access(student)
            elif new_status == "ACTIVE" and previous == "WITHDRAWN":
                _reinstate_access(student)

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    # SCD2: an edit closes the prior version and opens a new current one.
    from app.services.student_history_service import record_and_commit
    record_and_commit(student, "UPDATE")

    # Realtime: notify other clients of the update.
    from app.services.realtime_service import emit_entity_change
    emit_entity_change(institution_id, "student", "updated",
                       {"id": str(student.id), "admission_number": student.admission_number})

    return jsonify({
        "success": True,
        "data": _serialize_student(student),
        "message": "Student updated successfully."
    }), 200


@students_bp.route("/<uuid:student_id>", methods=["DELETE"])
def delete_student(student_id):
    """Soft-delete a student and revoke all access in a single transaction.

    This closes the "ghost login" hole: previously only ``status`` was set to
    WITHDRAWN while the linked ``users`` row stayed active, so the deleted
    student could still authenticate. Deleting now (a) soft-deletes and
    withdraws the student record, (b) deactivates and soft-deletes the linked
    login account, and (c) deactivates active enrollments so the student leaves
    every class roster. Any failure rolls the whole change back.
    """
    institution_id = get_current_tenant_id()
    query = Student.query.filter_by(id=student_id)
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    student = query.first()
    if not student or student.is_deleted:
        raise APIException("Student record not found", status_code=404, error_code="STUDENT_NOT_FOUND")

    try:
        # 1. Soft-delete + withdraw the student record itself.
        student.status = "WITHDRAWN"
        student.is_deleted = True

        # 2. Revoke the linked login account so the student can no longer sign in.
        if student.user:
            student.user.is_active = False
            student.user.is_deleted = True

        # 3. Deactivate active enrollments so the student drops off class rosters.
        for enrollment in student.enrollments:
            if enrollment.status == "ACTIVE":
                enrollment.status = "INACTIVE"

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    # SCD2: record a final DELETE version capturing the withdrawn/soft-deleted state.
    from app.services.student_history_service import record_and_commit
    record_and_commit(student, "DELETE")

    # Realtime: tell other clients to drop this student from their lists live.
    from app.services.realtime_service import emit_entity_change
    emit_entity_change(institution_id, "student", "deleted",
                       {"id": str(student.id), "admission_number": student.admission_number})

    return jsonify({
        "success": True,
        "message": f"Student {student.admission_number} withdrawn and account access revoked."
    }), 200


@students_bp.route("/<uuid:student_id>/restore", methods=["POST"])
@role_required("school_admin")
def restore_student(student_id):
    """Undo a soft delete (or a withdrawal): the exact inverse of DELETE.

    Restores the student record (is_deleted False, status ACTIVE), re-enables and
    un-deletes the linked login, and re-activates the latest enrollment so the
    student returns to the class roster — all in one transaction.
    """
    if not current_app.config.get("DATABASE_ENABLED", False):
        raise APIException("Database is not enabled.", status_code=503, error_code="DATABASE_UNAVAILABLE")

    institution_id = get_current_tenant_id()
    query = Student.query.filter(Student.id == student_id)
    if institution_id:
        query = query.filter(Student.institution_id == institution_id)

    student = query.first()
    if not student:
        raise APIException("Student record not found", status_code=404, error_code="STUDENT_NOT_FOUND")
    if not student.is_deleted and student.status != "WITHDRAWN":
        raise APIException(
            "Student is not deleted or withdrawn; nothing to restore.",
            status_code=409, error_code="STUDENT_NOT_DELETED",
        )

    try:
        student.is_deleted = False
        student.status = "ACTIVE"
        _reinstate_access(student)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    from app.services.student_history_service import record_and_commit
    record_and_commit(student, "RESTORE")

    from app.services.realtime_service import emit_entity_change
    emit_entity_change(institution_id or student.institution_id, "student", "restored",
                       {"id": str(student.id), "admission_number": student.admission_number})

    return jsonify({
        "success": True,
        "data": _serialize_student(student),
        "message": f"Student {student.admission_number} restored and account access re-enabled.",
    }), 200


@students_bp.route("/<uuid:student_id>/permanent", methods=["DELETE"])
@role_required("school_admin")
def purge_student(student_id):
    """Hard-delete an already soft-deleted student, in one transaction.

    Refuses (409) while the student is live, and refuses when the student still
    owns financial or academic records (fee invoices, attendance, exam results,
    assignment submissions, awards) — those must never be cascaded away. Links
    (guardians, enrollments, transport, history) go with the student. The linked
    login is removed too when nothing else references it; otherwise it stays
    deactivated.
    """
    if not current_app.config.get("DATABASE_ENABLED", False):
        raise APIException("Database is not enabled.", status_code=503, error_code="DATABASE_UNAVAILABLE")

    from app.models import (
        FeeInvoice, StudentAttendance, ExamResult, AssignmentSubmission, StudentAward,
        Parent, StaffProfile,
    )
    from app.models.user import UserRole

    institution_id = get_current_tenant_id()
    query = Student.query.filter(Student.id == student_id)
    if institution_id:
        query = query.filter(Student.institution_id == institution_id)

    student = query.first()
    if not student:
        raise APIException("Student record not found", status_code=404, error_code="STUDENT_NOT_FOUND")
    if not student.is_deleted:
        raise APIException(
            "Only a deleted student can be permanently removed. Delete the student first.",
            status_code=409, error_code="STUDENT_NOT_DELETED",
        )

    enrollment_ids = [e.id for e in student.enrollments]
    blockers = {
        "fee_invoices": FeeInvoice.query.filter(FeeInvoice.student_id == student.id).count(),
        "assignment_submissions": AssignmentSubmission.query.filter(
            AssignmentSubmission.student_id == student.id).count(),
        "awards": StudentAward.query.filter(StudentAward.student_id == student.id).count(),
        "attendance_records": (
            StudentAttendance.query.filter(StudentAttendance.enrollment_id.in_(enrollment_ids)).count()
            if enrollment_ids else 0
        ),
        "exam_results": (
            ExamResult.query.filter(ExamResult.enrollment_id.in_(enrollment_ids)).count()
            if enrollment_ids else 0
        ),
    }
    blockers = {k: v for k, v in blockers.items() if v}
    if blockers:
        raise APIException(
            "This student still has financial or academic records ("
            + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in blockers.items())
            + "). These are kept for audit; the student stays soft-deleted.",
            status_code=409, error_code="STUDENT_HAS_RECORDS", details=blockers,
        )

    admission_number = student.admission_number
    sid = str(student.id)
    owner_institution = student.institution_id
    user_id = student.user_id
    try:
        db.session.delete(student)
        db.session.flush()

        # Drop the login only when it belongs to this student alone.
        if user_id:
            shared = (
                Student.query.filter(Student.user_id == user_id).first()
                or Parent.query.filter(Parent.user_id == user_id).first()
                or StaffProfile.query.filter(StaffProfile.user_id == user_id).first()
            )
            if not shared:
                savepoint = db.session.begin_nested()
                try:
                    db.session.execute(sa_delete(UserRole).where(UserRole.user_id == user_id))
                    db.session.execute(sa_delete(User).where(User.id == user_id))
                    savepoint.commit()
                except IntegrityError:
                    # Referenced elsewhere (audit rows etc.): keep it deactivated.
                    savepoint.rollback()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    from app.services.realtime_service import emit_entity_change
    emit_entity_change(institution_id or owner_institution, "student", "purged",
                       {"id": sid, "admission_number": admission_number})

    return jsonify({
        "success": True,
        "message": f"Student {admission_number} permanently deleted.",
    }), 200
