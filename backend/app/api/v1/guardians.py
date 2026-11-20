import uuid
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import or_, and_
from app.models import Parent, Student, StudentParent, StudentEnrollment
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException

guardians_bp = Blueprint("guardians", __name__, url_prefix="/guardians")


def _serialize_guardian(parent: Parent):
    """Serialize a guardian with their linked (non-deleted) students, so the
    Guardian list cross-links straight to the Student / Class lists."""
    students = []
    for sp in parent.student_parents:
        s = sp.student
        if not s or s.is_deleted:
            continue
        active = next(
            (e for e in s.enrollments if e.status == "ACTIVE"),
            s.enrollments[0] if s.enrollments else None,
        )
        class_name = (
            active.section.class_level.name
            if active and active.section and active.section.class_level
            else None
        )
        students.append({
            "id": str(s.id),
            "admission_number": s.admission_number,
            "full_name": (f"{s.user.first_name} {s.user.last_name}".strip() if s.user else "Student"),
            "class_name": class_name,
            "section_id": str(active.section_id) if active and active.section_id else None,
            "section_name": active.section.name if active and active.section else None,
            "is_primary_contact": sp.is_primary_contact,
        })

    return {
        "id": str(parent.id),
        "first_name": parent.first_name,
        "last_name": parent.last_name,
        "full_name": f"{parent.first_name} {parent.last_name}".strip(),
        "relationship_type": parent.relationship_type,
        "occupation": parent.occupation,
        "email": parent.email or (parent.user.email if parent.user else None),
        "phone": parent.phone or (parent.user.phone if parent.user else None),
        "address": parent.address,
        "student_count": len(students),
        "students": students,
    }


@guardians_bp.route("", methods=["GET"])
def list_guardians():
    """List guardians/parents with search, section filter, and pagination.

    Cross-linked: each guardian carries their linked students (id, name, class,
    section). Soft-deleted guardians are excluded.
    """
    if not current_app.config.get("DATABASE_ENABLED", False):
        raise APIException("Database is not enabled.", status_code=503, error_code="DATABASE_UNAVAILABLE")

    institution_id = get_current_tenant_id()
    query = Parent.query.filter(Parent.is_deleted.is_(False))
    if institution_id:
        query = query.filter(Parent.institution_id == institution_id)

    search = request.args.get("search", "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(or_(
            Parent.first_name.ilike(like),
            Parent.last_name.ilike(like),
            Parent.phone.ilike(like),
            Parent.email.ilike(like),
        ))

    # Filter to guardians of students in a specific section.
    section_id = request.args.get("section_id")
    if section_id:
        try:
            sec_uuid = uuid.UUID(section_id)
        except (ValueError, TypeError):
            raise APIException("Invalid section_id", status_code=400, error_code="INVALID_SECTION_ID")
        query = query.filter(Parent.student_parents.any(
            StudentParent.student.has(and_(
                Student.is_deleted.is_(False),
                Student.enrollments.any(and_(
                    StudentEnrollment.section_id == sec_uuid,
                    StudentEnrollment.status == "ACTIVE",
                )),
            ))
        ))

    # Capped so (page - 1) * limit can never overflow the BIGINT OFFSET.
    page = min(1_000_000, max(1, request.args.get("page", 1, type=int)))
    limit = min(100, max(1, request.args.get("limit", 50, type=int)))
    total = query.count()
    parents = (
        query.order_by(Parent.first_name.asc(), Parent.last_name.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    return jsonify({
        "success": True,
        "data": {
            "items": [_serialize_guardian(p) for p in parents],
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit,
        }
    }), 200
