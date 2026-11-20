import json
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity, get_jwt
from app.extensions import db
from app.models import QuestionPaper, QuestionPaperVersion, Subject, ClassLevel
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException

question_papers_bp = Blueprint("question_papers", __name__, url_prefix="/question-papers")

# Moderation workflow. Authors move DRAFT -> SUBMITTED (or withdraw back to
# DRAFT); only a school admin reviews, approves, rejects or archives.
PAPER_TRANSITIONS = {
    "DRAFT": {"SUBMITTED"},
    "SUBMITTED": {"DRAFT", "IN_REVIEW", "APPROVED", "REJECTED"},
    "IN_REVIEW": {"APPROVED", "REJECTED"},
    "REJECTED": {"DRAFT"},
    "APPROVED": {"ARCHIVED"},
    "ARCHIVED": set(),
}
AUTHOR_TARGETS = {"DRAFT", "SUBMITTED"}
REVIEWER_ROLES = {"school_admin", "super_admin"}


def _uuid(value, field):
    if not isinstance(value, str):
        raise APIException(f"{field} must be a valid UUID", status_code=400, error_code="INVALID_ID")
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise APIException(f"{field} must be a valid UUID", status_code=400, error_code="INVALID_ID")


def _roles():
    try:
        return set((get_jwt() or {}).get("roles") or [])
    except Exception:
        return set()


def _actor_id():
    identity = get_jwt_identity()
    if not identity:
        raise APIException("Authentication required", status_code=401, error_code="AUTH_REQUIRED")
    return uuid.UUID(str(identity))


@question_papers_bp.route("", methods=["GET"])
def list_question_papers():
    institution_id = get_current_tenant_id()
    query = QuestionPaper.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status.upper())

    papers = query.order_by(QuestionPaper.created_at.desc()).all()
    data = [
        {
            "id": str(p.id),
            "title": p.title,
            "paper_code": p.paper_code,
            "subject_id": str(p.subject_id),
            "subject_name": p.subject.name if p.subject else None,
            "class_level_id": str(p.class_level_id),
            "class_name": p.class_level.name if p.class_level else None,
            "author_teacher_id": str(p.author_teacher_id),
            "author_name": f"{p.author.first_name} {p.author.last_name}".strip() if p.author else None,
            "status": p.status,
            "total_marks": float(p.total_marks),
            "time_allowed_minutes": p.time_allowed_minutes,
            "versions_count": len(p.versions),
        }
        for p in papers
    ]
    return jsonify({"success": True, "data": data}), 200


@question_papers_bp.route("/<uuid:paper_id>", methods=["GET"])
def get_question_paper(paper_id):
    institution_id = get_current_tenant_id(required=True)
    paper = QuestionPaper.query.filter_by(id=paper_id, institution_id=institution_id).first()
    if not paper:
        raise APIException("Question paper not found", status_code=404)

    return jsonify({
        "success": True,
        "data": {
            "id": str(paper.id),
            "title": paper.title,
            "paper_code": paper.paper_code,
            "status": paper.status,
            "total_marks": float(paper.total_marks),
            "time_allowed_minutes": paper.time_allowed_minutes,
            "versions": [
                {
                    "id": str(v.id),
                    "version_number": v.version_number,
                    "content_text": v.content_text,
                    "change_summary": v.change_summary,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in sorted(paper.versions, key=lambda x: x.version_number, reverse=True)
            ],
            "reviews": [
                {
                    "id": str(r.id),
                    "decision": r.review_decision,
                    "comments": r.comments,
                    "reviewed_at": r.review_date.isoformat() if r.review_date else None,
                }
                for r in paper.reviews
            ]
        }
    }), 200


@question_papers_bp.route("", methods=["POST"])
def create_question_paper():
    institution_id = get_current_tenant_id(required=True)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)

    title = payload.get("title")
    subject_id = payload.get("subject_id")
    class_level_id = payload.get("class_level_id")

    if not title or not subject_id or not class_level_id:
        raise APIException("title, subject_id, and class_level_id are required", status_code=400)
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 255:
        raise APIException("title must be a non-empty string (max 255 characters)", status_code=400)

    subject_uuid = _uuid(subject_id, "subject_id")
    class_uuid = _uuid(class_level_id, "class_level_id")
    if not Subject.query.filter_by(id=subject_uuid, institution_id=institution_id).first():
        raise APIException("Subject not found", status_code=404)
    if not ClassLevel.query.filter_by(id=class_uuid, institution_id=institution_id).first():
        raise APIException("Class not found", status_code=404)

    author_id = _actor_id()

    paper_code = payload.get("paper_code")
    if paper_code is not None and (not isinstance(paper_code, str) or len(paper_code.strip()) > 50):
        raise APIException("paper_code must be a string (max 50 characters)", status_code=400)
    paper_code = (paper_code or "").strip() or \
        f"QP-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

    raw_marks = payload.get("total_marks", 100.0)
    try:
        if isinstance(raw_marks, bool) or not isinstance(raw_marks, (int, float, str)):
            raise InvalidOperation
        total_marks = Decimal(str(raw_marks)).quantize(Decimal("0.01"))
        if not total_marks.is_finite() or not (0 < total_marks <= Decimal("999.99")):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise APIException("total_marks must be a number between 0 and 999.99", status_code=400)

    minutes = payload.get("time_allowed_minutes", 180)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not (1 <= minutes <= 1440):
        raise APIException("time_allowed_minutes must be an integer between 1 and 1440", status_code=400)

    # The version model stores text; structured content is serialised as JSON.
    content = payload.get("content_text")
    if content is None:
        content = payload.get("content_json")
    if content is None:
        content = {"sections": [], "instructions": "Read all questions carefully."}
    if isinstance(content, (dict, list)):
        content_text = json.dumps(content)
    elif isinstance(content, str):
        content_text = content
    else:
        raise APIException("content_json must be an object, list or string", status_code=400)

    paper = QuestionPaper(
        institution_id=institution_id,
        subject_id=subject_uuid,
        class_level_id=class_uuid,
        author_teacher_id=author_id,
        title=title.strip(),
        paper_code=paper_code,
        status="DRAFT",
        total_marks=total_marks,
        time_allowed_minutes=minutes,
    )
    db.session.add(paper)
    db.session.flush()

    v1 = QuestionPaperVersion(
        paper_id=paper.id,
        version_number=1,
        content_text=content_text,
        change_summary="Initial Draft creation",
        created_by=author_id,
    )
    db.session.add(v1)
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {"id": str(paper.id), "paper_code": paper.paper_code, "status": paper.status},
        "message": f"Question paper '{paper.title}' created."
    }), 201


@question_papers_bp.route("/<uuid:paper_id>/status", methods=["PUT"])
def transition_question_paper_status(paper_id):
    institution_id = get_current_tenant_id(required=True)
    paper = (
        QuestionPaper.query
        .filter_by(id=paper_id, institution_id=institution_id)
        .with_for_update()
        .first()
    )
    if not paper:
        raise APIException("Question paper not found", status_code=404)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    raw_status = payload.get("status")
    if not isinstance(raw_status, str) or not raw_status.strip():
        raise APIException("status is required", status_code=400)
    new_status = raw_status.strip().upper()
    if new_status not in PAPER_TRANSITIONS:
        raise APIException(f"Invalid status. Must be one of: {', '.join(sorted(PAPER_TRANSITIONS))}", status_code=400)

    current = (paper.status or "DRAFT").upper()
    if new_status == current:
        return jsonify({
            "success": True,
            "data": {"id": str(paper.id), "status": paper.status},
            "message": f"Question paper is already {current}."
        }), 200
    if new_status not in PAPER_TRANSITIONS.get(current, set()):
        raise APIException(f"Cannot move a question paper from {current} to {new_status}",
                           status_code=409, error_code="INVALID_STATUS_TRANSITION")

    is_reviewer = bool(_roles() & REVIEWER_ROLES)
    if not is_reviewer:
        # Teachers may only submit/withdraw their own papers; they can never
        # review, approve or reject (including their own).
        if new_status not in AUTHOR_TARGETS:
            raise APIException("Only a school admin can review, approve or reject question papers",
                               status_code=403, error_code="FORBIDDEN_ROLE")
        if paper.author_teacher_id != _actor_id():
            raise APIException("Only the author can submit or withdraw this question paper",
                               status_code=403, error_code="FORBIDDEN")

    paper.status = new_status
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {"id": str(paper.id), "status": paper.status},
        "message": f"Question paper transitioned to {new_status}."
    }), 200
