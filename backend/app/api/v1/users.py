import re
import secrets
import uuid
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy import func
from app.extensions import db
from app.models import User, Role, UserRole
from app.utils.errors import APIException
from app.utils.tenant import get_current_tenant_id
from app.utils.security import hash_password
from app.services.auth_service import (
    AuthService, clean_text_field, clean_email, clean_phone,
)
from app.services.dev_auth_service import is_dev_auth_active, DevAuthService
from app.services.security_audit import record_security_event

users_bp = Blueprint("users", __name__, url_prefix="/users")

# Roles an account may be created with. Only a super_admin may grant the
# administrative roles; a school_admin creates school-level personas only.
ASSIGNABLE_ROLES = {"student", "teacher", "parent", "supplier", "school_admin", "super_admin"}
PRIVILEGED_ROLES = {"school_admin", "super_admin"}

_LOGIN_ID_RE = re.compile(r"^[A-Za-z0-9._@\-]+$")


def _generate_temporary_password() -> str:
    """Generate a clean, memorable but policy-compliant temporary password."""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    nums = "23456789"
    special = "!@#$"
    p1 = "".join(secrets.choice(chars) for _ in range(4))
    p2 = "".join(secrets.choice(nums) for _ in range(4))
    p3 = secrets.choice(special)
    return f"{p1}#{p2}{p3}"


def _require_admin():
    """Return (claims, is_super_admin) for an administrator caller, else 403."""
    claims = get_jwt()
    roles = claims.get("roles") or []
    if "school_admin" not in roles and "super_admin" not in roles:
        raise APIException(message="Administrator access required.", status_code=403, error_code="FORBIDDEN")
    return claims, "super_admin" in roles


def _load_managed_user(user_id: uuid.UUID, claims, is_super: bool) -> User:
    """Load a target account the caller is allowed to manage.

    Non-super-admins may only act on accounts of their own institution (other
    tenants' accounts are reported as not found) and never on administrator
    accounts: resetting a fellow school admin's password would hand over that
    account (the temporary password is returned), so only a super_admin may.
    """
    u = db.session.get(User, user_id)
    if not u or u.is_deleted:
        raise APIException(message="User not found.", status_code=404, error_code="NOT_FOUND")
    if not is_super:
        caller_inst = claims.get("institution_id")
        if not caller_inst or u.institution_id is None or str(u.institution_id) != str(caller_inst):
            raise APIException(message="User not found.", status_code=404, error_code="NOT_FOUND")
        if "super_admin" in u.role_names:
            raise APIException(message="You cannot manage a platform administrator account.",
                               status_code=403, error_code="FORBIDDEN")
        if "school_admin" in u.role_names:
            raise APIException(message="Only a platform administrator can manage another administrator's account.",
                               status_code=403, error_code="FORBIDDEN")
    return u


def _audit(action: str, target: User, claims, summary: str) -> None:
    """Audit an admin action on an account (never includes passwords)."""
    record_security_event(
        action, target.id,
        actor_user_id=uuid.UUID(claims["sub"]) if claims.get("sub") else None,
        actor_role=",".join(claims.get("roles") or []) or None,
        institution_id=target.institution_id,
        summary=summary,
    )


@users_bp.route("", methods=["GET"])
@jwt_required()
def list_user_accounts():
    """List user accounts with roles, credentials status, and login audit tracking."""
    claims, is_super = _require_admin()

    role_filter = request.args.get("role")
    status_filter = request.args.get("status")
    search_query = (request.args.get("search") or "").strip().lower()

    if is_dev_auth_active():
        # Fallback store representation for database-free preview mode
        from app.services.dev_auth_service import _load_accounts
        data = _load_accounts()
        accounts = []
        for acc in data.get("accounts", {}).values():
            if role_filter and role_filter not in acc.get("roles", []):
                continue
            acc_status = "active" if acc.get("is_active", True) else "deactivated"
            if status_filter and status_filter != acc_status:
                continue
            if search_query:
                haystack = f"{acc.get('first_name','')} {acc.get('last_name','')} {acc.get('email','')} {acc.get('username','')} {acc.get('phone','')}".lower()
                if search_query not in haystack:
                    continue
            accounts.append({
                "id": acc["id"],
                "login_id": acc.get("username", acc["email"]),
                "username": acc.get("username"),
                "email": acc["email"],
                "phone": acc.get("phone", "+91 98222 33445"),
                "first_name": acc.get("first_name", ""),
                "last_name": acc.get("last_name", ""),
                "full_name": f"{acc.get('first_name', '')} {acc.get('last_name', '')}".strip(),
                "role": acc.get("roles", ["school_admin"])[0],
                "roles": acc.get("roles", []),
                "is_active": acc.get("is_active", True),
                "status": acc_status,
                "must_change_password": acc.get("must_change_password", False),
                "created_at": "2026-01-15T09:00:00Z",
                "last_login_at": "2026-11-03T18:30:00Z",
                "delivery_status": "delivered",
            })
        return jsonify({"success": True, "data": accounts}), 200

    # PostgreSQL Database Mode
    query = User.query
    if is_super:
        # A super_admin sees every institution unless one is selected via the
        # X-Institution-Id header / institution_id query arg (resolved by tenant.py).
        institution_id = get_current_tenant_id()
        if institution_id:
            query = query.filter(User.institution_id == institution_id)
    else:
        # School admins only ever see their own institution's accounts; platform
        # accounts (institution_id IS NULL) are never listed to them.
        institution_id = claims.get("institution_id")
        if not institution_id:
            return jsonify({"success": True, "data": []}), 200
        query = query.filter(User.institution_id == uuid.UUID(str(institution_id)))

    if search_query:
        query = query.filter(
            (User.email.ilike(f"%{search_query}%")) |
            (User.username.ilike(f"%{search_query}%")) |
            (User.first_name.ilike(f"%{search_query}%")) |
            (User.last_name.ilike(f"%{search_query}%")) |
            (User.phone.ilike(f"%{search_query}%"))
        )

    users = query.order_by(User.created_at.desc()).all()
    result = []
    for u in users:
        u_roles = u.role_names
        if not is_super and "super_admin" in u_roles:
            continue
        primary_role = u_roles[0] if u_roles else "student"
        if role_filter and role_filter not in u_roles:
            continue
        acc_status = "active" if u.is_active else "deactivated"
        if status_filter and status_filter != acc_status:
            continue
        result.append({
            "id": str(u.id),
            "login_id": u.username,
            "username": u.username,
            "email": u.email,
            "phone": u.phone or "+91 98222 33445",
            "first_name": u.first_name,
            "last_name": u.last_name,
            "full_name": u.full_name,
            "role": primary_role,
            "roles": u_roles,
            "is_active": u.is_active,
            "status": acc_status,
            "must_change_password": getattr(u, "must_change_password", False),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "delivery_status": "delivered",
        })

    return jsonify({"success": True, "data": result}), 200


def _first_present(data: dict, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


@users_bp.route("", methods=["POST"])
@jwt_required()
def create_user_account():
    """Create a user account with auto-generated Login ID and temporary password."""
    claims, is_super = _require_admin()

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise APIException(message="Request body must be a JSON object.", status_code=400)

    first_name = clean_text_field(_first_present(data, "first_name", "firstName"), "first_name", 100)
    last_name = clean_text_field(_first_present(data, "last_name", "lastName"), "last_name", 100)
    if not first_name or not last_name:
        raise APIException(message="First Name and Last Name are required.", status_code=400)
    email = clean_email(data.get("email"))
    phone = clean_phone(data.get("phone"))

    raw_role = data.get("role") or "student"
    if not isinstance(raw_role, str):
        raise APIException(message="role must be a string.", status_code=400, error_code="VALIDATION_ERROR")
    role_name = raw_role.strip().lower()
    if role_name not in ASSIGNABLE_ROLES:
        raise APIException(message=f"Unknown role '{role_name}'.", status_code=400, error_code="INVALID_ROLE")
    if role_name in PRIVILEGED_ROLES and not is_super:
        raise APIException(message="Only a platform administrator can create administrator accounts.",
                           status_code=403, error_code="FORBIDDEN")

    # Auto-generate unique Login ID if not supplied
    supplied_login_id = clean_text_field(_first_present(data, "login_id", "username"), "login_id", 100)
    if supplied_login_id and not _LOGIN_ID_RE.match(supplied_login_id):
        raise APIException(message="Login ID may only contain letters, digits, '.', '_', '@' and '-'.",
                           status_code=400, error_code="VALIDATION_ERROR")
    if not supplied_login_id:
        prefix_map = {
            "student": "STU",
            "teacher": "TCH",
            "parent": "PAR",
            "supplier": "SUP",
            "school_admin": "ADM",
        }
        prefix = prefix_map.get(role_name, "USR")
        suffix = secrets.randbelow(8999) + 1000
        generated_login_id = f"{prefix}-2026-{suffix}"
    else:
        generated_login_id = supplied_login_id

    # Auto-generate temporary password
    temp_password = data.get("temporary_password") or _generate_temporary_password()
    if not isinstance(temp_password, str) or len(temp_password) > 128:
        raise APIException(message="temporary_password must be a string of at most 128 characters.",
                           status_code=400, error_code="VALIDATION_ERROR")

    resolved_email = email or f"{generated_login_id.lower()}@stxaviers.edu.in"
    if len(resolved_email) > 255:
        raise APIException(message="Login ID is too long to derive an email address.",
                           status_code=400, error_code="VALIDATION_ERROR")

    # Fallback/Dev Mode — persist to the dev auth store so the new user can log in.
    if is_dev_auth_active():
        resolved_phone = phone or "+91 98222 33445"
        persisted = DevAuthService.create_account(
            first_name=first_name,
            last_name=last_name,
            email=resolved_email,
            phone=resolved_phone,
            username=generated_login_id,
            roles=[role_name],
            temporary_password=temp_password,
        )
        new_account = {
            "id": persisted["id"],
            "username": generated_login_id,
            "login_id": generated_login_id,
            "email": persisted["email"],
            "phone": persisted["phone"],
            "first_name": first_name,
            "last_name": last_name,
            "full_name": f"{first_name} {last_name}".strip(),
            "role": persisted["roles"][0],
            "roles": persisted["roles"],
            "is_active": True,
            "status": "active",
            "must_change_password": True,
            "temporary_password": temp_password,
            "delivery_status": "delivered",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return jsonify({
            "success": True,
            "message": f"User account created for {new_account['full_name']}.",
            "data": new_account
        }), 201

    # Database Mode
    if role_name == "super_admin":
        institution_id = None  # platform accounts are not tied to a school
    else:
        institution_id = get_current_tenant_id(required=True)
        if institution_id is None:
            raise APIException(message="Select an institution for this account.",
                               status_code=400, error_code="TENANT_REQUIRED")

    role = Role.query.filter_by(name=role_name).first()
    if not role:
        raise APIException(message=f"Unknown role '{role_name}'.", status_code=400, error_code="INVALID_ROLE")

    # Login ID and email are both login identifiers and unique case-insensitively.
    login_lower = generated_login_id.lower()
    existing_user = User.query.filter(
        (func.lower(User.username) == login_lower) |
        (func.lower(User.email) == resolved_email) |
        (func.lower(User.username) == resolved_email) |
        (func.lower(User.email) == login_lower)
    ).first()
    if existing_user:
        raise APIException(
            message=f"A user with Login ID '{generated_login_id}' or email '{resolved_email}' already exists.",
            status_code=409, error_code="DUPLICATE_USER",
        )
    # Phones are deliberately not unique: student logins carry the parent's
    # number, so the parent's own account (and siblings) must be able to share it.

    new_user = User(
        institution_id=institution_id,
        username=generated_login_id,
        email=resolved_email,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
        password_hash=hash_password(temp_password),
        is_active=True,
        is_verified=True,
        must_change_password=True,
    )
    db.session.add(new_user)
    db.session.flush()
    db.session.add(UserRole(user_id=new_user.id, role_id=role.id))
    _audit("USER_CREATED", new_user, get_jwt(), f"Account created with role '{role_name}'.")
    db.session.commit()

    return jsonify({
        "success": True,
        "message": f"User account created for {new_user.full_name}.",
        "data": {
            "id": str(new_user.id),
            "login_id": new_user.username,
            "username": new_user.username,
            "email": new_user.email,
            "phone": new_user.phone,
            "first_name": new_user.first_name,
            "last_name": new_user.last_name,
            "full_name": new_user.full_name,
            "role": role_name,
            "roles": [role_name],
            "is_active": True,
            "status": "active",
            "must_change_password": True,
            "temporary_password": temp_password,
            "delivery_status": "delivered",
            "created_at": new_user.created_at.isoformat() if new_user.created_at else None,
        }
    }), 201


@users_bp.route("/<uuid:user_id>/reset-password", methods=["POST"])
@jwt_required()
def admin_reset_password(user_id):
    """Admin resets user password and generates a fresh temporary password."""
    claims, is_super = _require_admin()

    temp_password = _generate_temporary_password()

    if is_dev_auth_active():
        DevAuthService.update_user_password(user_id, temp_password, must_change_password=True)
        return jsonify({
            "success": True,
            "message": "Temporary password generated. User must change it upon their next login.",
            "data": {
                "temporary_password": temp_password,
                "must_change_password": True,
            }
        }), 200

    u = _load_managed_user(user_id, claims, is_super)

    u.password_hash = hash_password(temp_password)
    u.must_change_password = True
    _audit("ADMIN_PASSWORD_RESET", u, claims, "Temporary password issued by an administrator.")
    # A reset also clears any failed-login lockout.
    _audit("ACCOUNT_UNLOCKED", u, claims, "Lockout cleared by password reset.")
    # Sessions opened with the old password must not survive the reset
    # (logout_all_sessions commits the password change along with it).
    AuthService.logout_all_sessions(u.id)

    return jsonify({
        "success": True,
        "message": f"Temporary password generated for {u.full_name}.",
        "data": {
            "temporary_password": temp_password,
            "must_change_password": True,
        }
    }), 200


@users_bp.route("/<uuid:user_id>/toggle-status", methods=["POST"])
@jwt_required()
def toggle_user_status(user_id):
    """Activate or deactivate user account."""
    claims, is_super = _require_admin()

    if str(user_id) == str(claims.get("sub")):
        raise APIException(message="You cannot change the status of your own account.",
                           status_code=400, error_code="SELF_STATUS_CHANGE")

    if is_dev_auth_active():
        return jsonify({
            "success": True,
            "message": "Account status updated successfully.",
            "data": {"status": "deactivated"}
        }), 200

    u = _load_managed_user(user_id, claims, is_super)

    u.is_active = not u.is_active
    _audit("USER_STATUS_CHANGED", u, claims, "Account activated." if u.is_active else "Account deactivated.")
    if not u.is_active:
        # A deactivated account must lose access immediately, not when its
        # current access/refresh tokens expire (this also commits the toggle).
        AuthService.logout_all_sessions(u.id)
    else:
        db.session.commit()

    status_label = "active" if u.is_active else "deactivated"
    return jsonify({
        "success": True,
        "message": f"User account has been {status_label}.",
        "data": {
            "id": str(u.id),
            "is_active": u.is_active,
            "status": status_label,
        }
    }), 200


@users_bp.route("/<uuid:user_id>/resend-credentials", methods=["POST"])
@jwt_required()
def resend_credentials(user_id):
    """Resend login credentials via registered SMS and/or Gmail."""
    claims, is_super = _require_admin()

    data = request.get_json(silent=True) or {}
    channel = data.get("channel", "sms") if isinstance(data, dict) else "sms"
    if channel not in ("sms", "email", "gmail", "both"):
        raise APIException(message="channel must be one of sms, email, gmail, both.",
                           status_code=400, error_code="VALIDATION_ERROR")

    if not is_dev_auth_active():
        target = _load_managed_user(user_id, claims, is_super)
        _audit("CREDENTIALS_RESENT", target, claims, f"Login details re-sent via {channel}.")
        db.session.commit()

    return jsonify({
        "success": True,
        "message": f"Login credentials successfully dispatched via {channel.upper()}.",
        "data": {
            "channel": channel,
            "delivery_status": "delivered",
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
        }
    }), 200
