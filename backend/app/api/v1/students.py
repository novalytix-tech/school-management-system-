import os
import time
import uuid
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import or_, and_, insert as sa_insert
from app.extensions import db
from app.models import (
    Student, StudentEnrollment, ClassLevel, Section, AcademicYear, User
)
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException

students_bp = Blueprint("students", __name__, url_prefix="/students")


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
        query = query.filter(Student.enrollments.any(
            and_(StudentEnrollment.section_id == uuid.UUID(section_id), StudentEnrollment.status == "ACTIVE")
        ))

    # Pagination
    page = max(1, request.args.get("page", 1, type=int))
    limit = min(100, max(1, request.args.get("limit", 50, type=int)))
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


_LOOKUP_CACHE = {"key": None, "at": 0.0, "value": None}
_LOOKUP_TTL = 60.0  # seconds


def _import_lookups(institution_id):
    """Sections map, student role id and active academic year — cached briefly."""
    from app.models.user import Role

    now = time.time()
    cache = _LOOKUP_CACHE
    if cache["value"] and cache["key"] == str(institution_id) and (now - cache["at"]) < _LOOKUP_TTL:
        return cache["value"]

    sections_map = {}
    sections_without_teacher = set()
    for sec_id, sec_name, cls_name, class_teacher_id in (
        db.session.query(Section.id, Section.name, ClassLevel.name, Section.class_teacher_id)
        .join(ClassLevel, Section.class_level_id == ClassLevel.id)
        .filter(ClassLevel.institution_id == institution_id)
        .all()
    ):
        sections_map[((cls_name or "").strip().lower(), (sec_name or "").strip().lower())] = sec_id
        if class_teacher_id is None:
            sections_without_teacher.add(sec_id)

    role = Role.query.filter_by(name="student").first()
    year = (
        AcademicYear.query.filter_by(institution_id=institution_id, is_current=True).first()
        or AcademicYear.query.filter_by(institution_id=institution_id).first()
    )

    value = (
        sections_map,
        role.id if role else None,
        year.id if year else None,
        year.name if year else "2026-2027",
        sections_without_teacher,
    )
    _LOOKUP_CACHE.update({"key": str(institution_id), "at": now, "value": value})
    return value


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
    body = request.get_json(silent=True) or {}
    rows = body.get("students") or body.get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise APIException("No student rows provided.", status_code=400, error_code="NO_ROWS")

    row_offset = int(body.get("rowOffset") or 0)  # keeps CSV row numbers correct across batches

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
    sections_map, student_role_id, year_id, year_name, sections_without_teacher = _import_lookups(institution_id)
    if not year_id:
        raise APIException(
            "No academic year is configured; cannot import students.",
            status_code=400, error_code="NO_ACADEMIC_YEAR",
        )

    # ---------- Validate + prepare entirely in memory ----------
    prepared, errors, import_warnings = [], [], set()
    for i, row in enumerate(rows):
        csv_row = row_offset + i + 2  # +2: header line plus 1-based numbering

        full_name = (row.get("fullName") or row.get("full_name") or row.get("name") or "").strip()
        if not full_name:
            errors.append({"row": csv_row, "name": "", "reason": "Name is missing"})
            continue

        # Integrity rule (Task 2): resolve the Class + Section up front. A row that
        # can't be mapped to an existing section is a surfaced error (skipped),
        # never a silent import of a student with no enrollment.
        class_name_val = (row.get("className") or row.get("class") or "").strip()
        section_val = (row.get("section") or "").strip()
        resolved_section_id = sections_map.get((class_name_val.lower(), section_val.lower()))
        if not resolved_section_id:
            errors.append({
                "row": csv_row,
                "name": full_name,
                "reason": f"Class/Section '{class_name_val or '—'} / {section_val or '—'}' not found. "
                          "Assign a valid existing class and section.",
            })
            continue
        if resolved_section_id in sections_without_teacher:
            import_warnings.add(f"{class_name_val} {section_val}".strip())

        parts = full_name.split(" ")
        first_name = (row.get("firstName") or parts[0] or full_name).strip()
        last_name = (row.get("lastName") or (" ".join(parts[1:]) if len(parts) > 1 else first_name)).strip()

        admission_no = (row.get("admissionNo") or row.get("admission_number") or "").strip()
        if not admission_no:
            admission_no = "ADM-" + secrets.token_hex(3).upper()
        if admission_no.lower() in existing_adm:
            errors.append({
                "row": csv_row,
                "name": full_name,
                "reason": "Duplicate admission number '" + admission_no + "'",
            })
            continue
        existing_adm.add(admission_no.lower())

        username = (row.get("loginId") or admission_no).strip()
        while username.lower() in existing_usernames:
            username = admission_no + "-" + secrets.token_hex(2)
        existing_usernames.add(username.lower())

        email = (row.get("parentEmail") or row.get("email") or "").strip().lower()
        if not email or email in existing_emails:
            email = username.lower() + "@student.local"
            while email in existing_emails:
                email = username.lower() + "-" + secrets.token_hex(2) + "@student.local"
        existing_emails.add(email)

        dob = row.get("dob") or row.get("date_of_birth")
        try:
            dob_val = datetime.strptime(str(dob)[:10], "%Y-%m-%d").date() if dob else date(2012, 1, 1)
        except ValueError:
            dob_val = date(2012, 1, 1)

        prepared.append({
            "full_name": full_name, "first_name": first_name, "last_name": last_name,
            "admission_no": admission_no, "username": username, "email": email,
            "dob": dob_val,
            "gender": (row.get("gender") or "OTHER"),
            "blood_group": row.get("bloodGroup") or row.get("blood_group"),
            "phone": row.get("parentPhone") or row.get("phone"),
            "class_name": class_name_val,
            "section": section_val,
            "section_id": resolved_section_id,
            "roll_no": row.get("rollNo") or row.get("roll_number"),
            "parent_name": row.get("parentName") or "Guardian",
            "parent_email": row.get("parentEmail") or "",
            "address": row.get("address") or "",
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
    except Exception as exc:
        db.session.rollback()
        raise APIException(
            "Database rejected the batch: " + str(exc).splitlines()[0][:160],
            status_code=400,
            error_code="IMPORT_DB_ERROR",
        )

    # SCD2: seed an initial CREATE version per imported student, in a separate
    # non-fatal transaction so a pending migration can't fail the committed import.
    from app.services.student_history_service import record_versions_bulk_and_commit
    record_versions_bulk_and_commit(history_inputs)

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
    payload = request.get_json() or {}

    admission_no = payload.get("admission_number") or payload.get("admissionNo")
    if not admission_no:
        raise APIException("Admission number is required", status_code=400, error_code="MISSING_ADMISSION_NO")

    # Check duplicate admission number
    exists = Student.query.filter_by(institution_id=institution_id, admission_number=admission_no.strip()).first()
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

    dob = payload.get("date_of_birth") or payload.get("dob")
    if dob:
        dob_val = datetime.strptime(str(dob)[:10], "%Y-%m-%d").date()
    else:
        dob_val = date(2010, 1, 1)

    adm_date = payload.get("admission_date")
    if adm_date:
        adm_date_val = datetime.strptime(str(adm_date)[:10], "%Y-%m-%d").date()
    else:
        adm_date_val = date.today()

    first_name = payload.get("first_name") or payload.get("firstName") or "Student"
    last_name = payload.get("last_name") or payload.get("lastName") or ""
    email = payload.get("email")

    user_obj = None
    if email:
        user_obj = User.query.filter_by(email=email.lower().strip()).first()
        if not user_obj:
            from app.utils.security import hash_password
            from app.models.user import Role, UserRole
            clean_email = email.lower().strip()
            username = clean_email.split('@')[0]
            # Ensure unique username
            existing_u = User.query.filter_by(username=username).first()
            if existing_u:
                username = f"{username}_{int(datetime.now().timestamp())}"

            user_obj = User(
                institution_id=institution_id,
                username=username,
                email=clean_email,
                password_hash=hash_password("SchoolERP@Demo2026!"),
                first_name=first_name,
                last_name=last_name,
                phone=payload.get("phone") or payload.get("phone_number"),
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
        admission_number=admission_no.strip(),
        admission_date=adm_date_val,
        date_of_birth=dob_val,
        gender=payload.get("gender", "OTHER").upper(),
        blood_group=payload.get("blood_group") or payload.get("bloodGroup"),
        nationality=payload.get("nationality", "Indian"),
        religion=payload.get("religion"),
        caste_category=payload.get("caste_category", "GENERAL"),
        emergency_contact_name=payload.get("emergency_contact_name") or payload.get("emergencyContact"),
        emergency_contact_phone=payload.get("emergency_contact_phone") or payload.get("emergencyPhone"),
        medical_notes=payload.get("medical_notes"),
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
        roll_number=payload.get("roll_number") or payload.get("rollNo"),
        enrollment_date=date.today(),
        status="ACTIVE",
    )
    db.session.add(enrollment)

    db.session.commit()

    # SCD2: record the initial version of the student (expire_on_commit means the
    # student now reflects its persisted state, including the enrollment).
    from app.services.student_history_service import record_and_commit
    record_and_commit(student, "CREATE")

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
    query = Student.query.filter_by(id=student_id)
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    student = query.first()
    if not student:
        raise APIException("Student record not found", status_code=404, error_code="STUDENT_NOT_FOUND")

    payload = request.get_json() or {}

    if "gender" in payload:
        student.gender = payload["gender"].upper()
    if "blood_group" in payload or "bloodGroup" in payload:
        student.blood_group = payload.get("blood_group") or payload.get("bloodGroup")
    if "nationality" in payload:
        student.nationality = payload["nationality"]
    if "emergency_contact_name" in payload:
        student.emergency_contact_name = payload["emergency_contact_name"]
    if "emergency_contact_phone" in payload:
        student.emergency_contact_phone = payload["emergency_contact_phone"]
    if "medical_notes" in payload:
        student.medical_notes = payload["medical_notes"]
    if "status" in payload:
        student.status = payload["status"].upper()

    if student.user:
        if "first_name" in payload or "firstName" in payload:
            student.user.first_name = payload.get("first_name") or payload.get("firstName")
        if "last_name" in payload or "lastName" in payload:
            student.user.last_name = payload.get("last_name") or payload.get("lastName")
        if "phone" in payload or "phone_number" in payload:
            student.user.phone = payload.get("phone") or payload.get("phone_number")

    db.session.commit()

    # SCD2: an edit closes the prior version and opens a new current one.
    from app.services.student_history_service import record_and_commit
    record_and_commit(student, "UPDATE")

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

    return jsonify({
        "success": True,
        "message": f"Student {student.admission_number} withdrawn and account access revoked."
    }), 200
