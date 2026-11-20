import uuid
from datetime import date, datetime
from flask import Blueprint, request, jsonify
from app.extensions import db
from app.models import (
    AdmissionInquiry, AdmissionApplication, AcademicYear, ClassLevel
)
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException
from app.utils.cache_helpers import bump_version

admissions_bp = Blueprint("admissions", __name__, url_prefix="/admissions")

# Allowed application status transitions. ENROLLED is terminal; a REJECTED
# application may be re-opened for review.
APPLICATION_TRANSITIONS = {
    "SUBMITTED": {"UNDER_REVIEW", "ASSESSMENT_SCHEDULED", "OFFERED", "REJECTED"},
    "UNDER_REVIEW": {"ASSESSMENT_SCHEDULED", "OFFERED", "REJECTED"},
    "ASSESSMENT_SCHEDULED": {"UNDER_REVIEW", "OFFERED", "REJECTED"},
    "OFFERED": {"ENROLLED", "REJECTED"},
    "ENROLLED": set(),
    "REJECTED": {"UNDER_REVIEW"},
}


def _json_body():
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    return payload


def _opt_str(payload, *keys, max_len=255):
    """First non-empty value among keys; must be a string when present."""
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or len(value.strip()) > max_len:
            raise APIException(f"{key} must be a string (max {max_len} characters)", status_code=400)
        if value.strip():
            return value.strip()
    return None


def _parse_dob(raw, field="date_of_birth"):
    if not isinstance(raw, str):
        raise APIException(f"{field} must be a date in YYYY-MM-DD format", status_code=400)
    try:
        return datetime.strptime(raw.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise APIException(f"{field} must be a date in YYYY-MM-DD format", status_code=400)


def _tenant_class(class_id, institution_id):
    """Resolve a class id supplied by the client, scoped to the tenant."""
    if not isinstance(class_id, str):
        raise APIException("class id must be a valid UUID", status_code=400, error_code="INVALID_ID")
    try:
        class_uuid = uuid.UUID(class_id.strip())
    except ValueError:
        raise APIException("class id must be a valid UUID", status_code=400, error_code="INVALID_ID")
    class_obj = ClassLevel.query.filter_by(id=class_uuid, institution_id=institution_id).first()
    if not class_obj:
        raise APIException("Class not found", status_code=404)
    return class_obj


# -------------------------------------------------------------
# 1. Inquiries
# -------------------------------------------------------------

@admissions_bp.route("/inquiries", methods=["GET"])
def list_inquiries():
    institution_id = get_current_tenant_id()
    query = AdmissionInquiry.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status.upper())

    inquiries = query.order_by(AdmissionInquiry.inquiry_date.desc()).all()
    data = [
        {
            "id": str(i.id),
            "applicant_name": i.applicant_name,
            "parent_name": i.parent_name,
            "target_class_level_id": str(i.target_class_level_id),
            "target_class_name": i.target_class.name if i.target_class else None,
            "inquiry_date": i.inquiry_date.isoformat(),
            "email": i.email,
            "phone": i.phone,
            "source": i.source,
            "status": i.status,
            "notes": i.notes,
            "applicant_dob": i.applicant_dob.isoformat() if i.applicant_dob else None,
            "gender": i.gender,
            "previous_school": i.previous_school,
            "previous_class": i.previous_class,
            "previous_board": i.previous_board,
            "interested_class_name": i.interested_class_name or (i.target_class.name if i.target_class else None),
            "address": i.address,
            "alternate_phone": i.alternate_phone,
            "relationship_to_applicant": i.relationship_to_applicant,
        }
        for i in inquiries
    ]
    return jsonify({"success": True, "data": data}), 200


@admissions_bp.route("/inquiries", methods=["POST"])
def create_inquiry():
    institution_id = get_current_tenant_id(required=True)
    payload = _json_body()

    first_name = _opt_str(payload, "first_name", "firstName", max_len=100) or "Applicant"
    last_name = _opt_str(payload, "last_name", "lastName", max_len=100) or ""
    phone = _opt_str(payload, "phone_number", "phone", max_len=50)
    parent_name = _opt_str(payload, "parent_name", max_len=100) or f"{first_name}'s Parent"[:100]
    applicant_name = _opt_str(payload, "applicant_name", max_len=100) or f"{first_name} {last_name}".strip()[:100]

    if not phone:
        raise APIException("phone is required", status_code=400)

    class_id = payload.get("target_class_level_id") or payload.get("class_level_id")
    class_obj = _tenant_class(class_id, institution_id) if class_id else None
    created_class = False
    if not class_obj:
        class_obj = ClassLevel.query.filter_by(institution_id=institution_id).first()
    if not class_obj:
        class_obj = ClassLevel(institution_id=institution_id, name="Grade 1", code="G1", sequence_order=1)
        db.session.add(class_obj)
        db.session.flush()
        created_class = True

    # Parse optional date of birth (accepts YYYY-MM-DD)
    dob_raw = payload.get("applicant_dob") or payload.get("dob")
    dob_val = _parse_dob(dob_raw, "applicant_dob") if dob_raw else None

    inquiry = AdmissionInquiry(
        institution_id=institution_id,
        parent_name=parent_name,
        applicant_name=applicant_name,
        target_class_level_id=class_obj.id,
        email=_opt_str(payload, "email", max_len=255),
        phone=phone,
        inquiry_date=date.today(),
        source=_opt_str(payload, "source", max_len=50) or "WEBSITE",
        status="NEW",
        notes=_opt_str(payload, "notes", max_len=5000),
        applicant_dob=dob_val,
        gender=_opt_str(payload, "gender", max_len=20),
        previous_school=_opt_str(payload, "previous_school", "previousSchool", max_len=255),
        previous_class=_opt_str(payload, "previous_class", "previousClass", max_len=50),
        previous_board=_opt_str(payload, "previous_board", "previousBoard", max_len=50),
        interested_class_name=_opt_str(payload, "interested_class_name", "interestedClassName", max_len=100),
        address=_opt_str(payload, "address", max_len=5000),
        alternate_phone=_opt_str(payload, "alternate_phone", "alternateMobile", max_len=50),
        relationship_to_applicant=_opt_str(payload, "relationship", "relationship_to_applicant", max_len=30),
    )
    db.session.add(inquiry)
    db.session.commit()
    if created_class:
        # The academics endpoints cache class lists; drop them for this tenant.
        bump_version("academics", str(institution_id))

    return jsonify({
        "success": True,
        "data": {
            "id": str(inquiry.id),
            "applicant_name": inquiry.applicant_name,
            "status": inquiry.status,
        },
        "message": "Inquiry recorded successfully."
    }), 201


# -------------------------------------------------------------
# 2. Applications
# -------------------------------------------------------------

@admissions_bp.route("/applications", methods=["GET"])
def list_applications():
    institution_id = get_current_tenant_id()
    query = AdmissionApplication.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status.upper())

    apps = query.order_by(AdmissionApplication.submission_date.desc()).all()
    data = [
        {
            "id": str(a.id),
            "application_number": a.application_number,
            "submission_date": a.submission_date.isoformat(),
            "applicant_first_name": a.applicant_first_name,
            "applicant_last_name": a.applicant_last_name,
            "full_name": f"{a.applicant_first_name} {a.applicant_last_name}".strip(),
            "date_of_birth": a.date_of_birth.isoformat(),
            "gender": a.gender,
            "status": a.status,
            "target_class_level_id": str(a.target_class_level_id),
            "target_class_name": a.target_class.name if a.target_class else None,
            "target_academic_year_id": str(a.target_academic_year_id),
            "target_academic_year_name": a.target_year.name if a.target_year else None,
        }
        for a in apps
    ]
    return jsonify({"success": True, "data": data}), 200


@admissions_bp.route("/applications", methods=["POST"])
def submit_application():
    institution_id = get_current_tenant_id(required=True)
    payload = _json_body()

    first_name = _opt_str(payload, "applicant_first_name", "first_name", "firstName", max_len=100)
    last_name = _opt_str(payload, "applicant_last_name", "last_name", "lastName", max_len=100) or ""

    if not first_name:
        raise APIException("applicant_first_name is required", status_code=400)

    # Resolve academic year
    ay = AcademicYear.query.filter_by(institution_id=institution_id, is_current=True).first()
    if not ay:
        ay = AcademicYear.query.filter_by(institution_id=institution_id).first()
    if not ay:
        raise APIException("No academic year found for institution", status_code=400)

    # Resolve target class
    class_id = payload.get("target_class_level_id") or payload.get("class_applied_id") or payload.get("classId")
    class_obj = _tenant_class(class_id, institution_id) if class_id else None
    if not class_obj:
        class_obj = ClassLevel.query.filter_by(institution_id=institution_id).first()
    if not class_obj:
        raise APIException("No class found for institution", status_code=400)

    dob = payload.get("date_of_birth") or payload.get("dob")
    dob_val = _parse_dob(dob) if dob else date(2012, 1, 1)

    gender = _opt_str(payload, "gender", max_len=20) or "OTHER"

    # A bare timestamp collides for two submissions in the same second.
    app_no = f"APP-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:10].upper()}"

    app = AdmissionApplication(
        institution_id=institution_id,
        target_academic_year_id=ay.id,
        target_class_level_id=class_obj.id,
        application_number=app_no,
        applicant_first_name=first_name,
        applicant_last_name=last_name,
        date_of_birth=dob_val,
        gender=gender.upper(),
        submission_date=date.today(),
        status="SUBMITTED",
    )
    db.session.add(app)
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {
            "id": str(app.id),
            "application_number": app.application_number,
            "status": app.status,
        },
        "message": f"Application {app.application_number} submitted successfully."
    }), 201


@admissions_bp.route("/applications/<uuid:application_id>/status", methods=["PUT"])
def update_application_status(application_id):
    institution_id = get_current_tenant_id(required=True)
    app = (
        AdmissionApplication.query
        .filter_by(id=application_id, institution_id=institution_id)
        .with_for_update()
        .first()
    )
    if not app:
        raise APIException("Application not found", status_code=404)

    payload = _json_body()
    raw_status = payload.get("status")
    if not isinstance(raw_status, str) or not raw_status.strip():
        raise APIException("status is required", status_code=400)
    new_status = raw_status.strip().upper()
    valid_statuses = sorted(APPLICATION_TRANSITIONS)
    if new_status not in APPLICATION_TRANSITIONS:
        raise APIException(f"Invalid status. Must be one of: {', '.join(valid_statuses)}", status_code=400)

    current = (app.status or "SUBMITTED").upper()
    if new_status != current:
        allowed = APPLICATION_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise APIException(
                f"Cannot move an application from {current} to {new_status}",
                status_code=409, error_code="INVALID_STATUS_TRANSITION",
            )
        app.status = new_status
        db.session.commit()

    return jsonify({
        "success": True,
        "data": {
            "id": str(app.id),
            "status": app.status,
        },
        "message": f"Application status updated to {new_status}."
    }), 200
