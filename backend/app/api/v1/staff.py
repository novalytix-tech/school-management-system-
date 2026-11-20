import uuid
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from app.extensions import db
from app.models import StaffProfile, Department, LeaveRequest, LeaveApproval
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException

staff_bp = Blueprint("staff", __name__, url_prefix="/staff")


# -------------------------------------------------------------
# 1. Staff Profiles
# -------------------------------------------------------------

@staff_bp.route("", methods=["GET"])
def list_staff():
    institution_id = get_current_tenant_id()
    query = StaffProfile.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status.upper())

    staff_members = query.all()
    data = []
    for s in staff_members:
        u = s.user
        data.append({
            "id": str(s.id),
            "user_id": str(s.user_id),
            "employee_id": s.employee_id,
            "first_name": u.first_name if u else "Staff",
            "last_name": u.last_name if u else "",
            "full_name": f"{u.first_name} {u.last_name}".strip() if u else "Staff Member",
            "email": u.email if u else None,
            "phone": u.phone if u else None,
            "designation": s.designation,
            "department_id": str(s.department_id) if s.department_id else None,
            "department_name": s.department.name if s.department else None,
            "joining_date": s.joining_date.isoformat(),
            "status": s.status,
        })
    return jsonify({"success": True, "data": data}), 200


@staff_bp.route("/departments", methods=["GET"])
def list_departments():
    institution_id = get_current_tenant_id()
    query = Department.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    depts = query.order_by(Department.name.asc()).all()
    data = [{"id": str(d.id), "name": d.name} for d in depts]
    return jsonify({"success": True, "data": data}), 200


# -------------------------------------------------------------
# 2. Leave Management
# -------------------------------------------------------------

@staff_bp.route("/leaves", methods=["GET"])
def list_leaves():
    institution_id = get_current_tenant_id()
    query = LeaveRequest.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    status = request.args.get("status")
    if status:
        query = query.filter_by(status=status.upper())

    leaves = query.order_by(LeaveRequest.start_date.desc()).all()
    data = [
        {
            "id": str(l.id),
            "applicant_id": str(l.applicant_user_id),
            "applicant_name": f"{l.applicant.first_name} {l.applicant.last_name}".strip() if l.applicant else None,
            "category_name": l.category.name if l.category else None,
            "start_date": l.start_date.isoformat(),
            "end_date": l.end_date.isoformat(),
            "total_days": float(l.total_days),
            "reason": l.reason,
            "status": l.status,
        }
        for l in leaves
    ]
    return jsonify({"success": True, "data": data}), 200


# Accept both the verb ("APPROVE") and the resulting state ("APPROVED").
_LEAVE_ACTIONS = {
    "APPROVE": "APPROVED",
    "APPROVED": "APPROVED",
    "REJECT": "REJECTED",
    "REJECTED": "REJECTED",
}


@staff_bp.route("/leaves/<uuid:leave_id>/action", methods=["PUT"])
def take_leave_action(leave_id):
    verify_jwt_in_request()
    approver_id = get_jwt_identity()
    if not approver_id:
        raise APIException("Authentication required", status_code=401, error_code="UNAUTHORIZED")

    institution_id = get_current_tenant_id(required=True)
    leave = LeaveRequest.query.filter_by(id=leave_id, institution_id=institution_id).first()
    if not leave:
        raise APIException("Leave request not found", status_code=404)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)

    raw_action = payload.get("action")
    action = _LEAVE_ACTIONS.get(raw_action.strip().upper()) if isinstance(raw_action, str) else None
    if not action:
        raise APIException("Action must be either APPROVE or REJECT", status_code=400)

    comments = payload.get("comments", payload.get("remarks"))
    if comments is not None and not isinstance(comments, str):
        raise APIException("comments must be a string", status_code=400)

    if leave.approval is not None or leave.status in {"APPROVED", "REJECTED"}:
        raise APIException("Leave request has already been decided", status_code=409, error_code="ALREADY_DECIDED")

    leave.status = action
    approval = LeaveApproval(
        leave_request_id=leave.id,
        approver_user_id=uuid.UUID(str(approver_id)),
        decision=action,
        remarks=comments,
    )
    db.session.add(approval)
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {"id": str(leave.id), "status": leave.status},
        "message": f"Leave request {action.lower()}."
    }), 200
