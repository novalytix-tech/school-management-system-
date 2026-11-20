import html
import uuid
from datetime import date, datetime, timezone
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity, get_jwt
from sqlalchemy import or_
from app.extensions import db
from app.models import Notice, Message, User
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException
from app.services.sms_service import sms_service
from app.services.email_service import email_service

communications_bp = Blueprint("communications", __name__, url_prefix="/communications")

MAILBOX_ADMIN_ROLES = {"school_admin", "super_admin"}


def _json_body():
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object", status_code=400)
    return payload


def _str_field(payload, *keys, default="", max_len=None):
    """First present value among keys, which must be a string."""
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise APIException(f"{key} must be a string", status_code=400)
        value = value.strip()
        if max_len and len(value) > max_len:
            raise APIException(f"{key} must be at most {max_len} characters", status_code=400)
        return value
    return default


def _uuid(value, field):
    if not isinstance(value, str):
        raise APIException(f"{field} must be a valid UUID", status_code=400, error_code="INVALID_ID")
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise APIException(f"{field} must be a valid UUID", status_code=400, error_code="INVALID_ID")


def _actor_id():
    """The authenticated user (never a 'first active user' fallback)."""
    identity = get_jwt_identity()
    if not identity:
        raise APIException("Authentication required", status_code=401, error_code="AUTH_REQUIRED")
    return uuid.UUID(str(identity))


# -------------------------------------------------------------
# 0. Credential delivery (SMS / Email) — works without the database
# -------------------------------------------------------------

@communications_bp.route("/send-credentials", methods=["POST"])
def send_credentials():
    """Deliver a new user's Login ID + temporary password over SMS and/or Email.

    Body: { name, role, login_id, temporary_password, phone?, email?, channels? }
    `channels` is a list like ["sms", "email"]; defaults to whatever contact info is present.
    Returns the real send result per channel so the UI can report success or failure honestly.
    """
    payload = _json_body()

    name = _str_field(payload, "name", "person_name", default="User", max_len=200)
    role = _str_field(payload, "role", default="user", max_len=50)
    login_id = _str_field(payload, "login_id", "loginId", max_len=255)
    temporary_password = _str_field(payload, "temporary_password", "temporaryPassword", max_len=255)
    phone = _str_field(payload, "phone", max_len=50)
    email = _str_field(payload, "email", max_len=255)

    if not login_id or not temporary_password:
        raise APIException(
            "login_id and temporary_password are required",
            status_code=400,
            error_code="CREDENTIALS_REQUIRED",
        )

    channels = payload.get("channels")
    if channels and (not isinstance(channels, list) or not all(isinstance(c, str) for c in channels)):
        raise APIException("channels must be a list of strings", status_code=400)
    if not channels:
        channels = []
        if phone:
            channels.append("sms")
        if email:
            channels.append("email")

    results = {}

    if "sms" in channels:
        if not phone:
            results["sms"] = {"sent": False, "detail": "No phone number on record."}
        else:
            results["sms"] = sms_service.send_credentials(
                phone=phone,
                person_name=name,
                login_id=login_id,
                temporary_password=temporary_password,
                role=role,
            )

    if "email" in channels:
        if not email:
            results["email"] = {"sent": False, "detail": "No email on record."}
        else:
            subject = "Your School ERP Login Credentials"
            text_body = (
                f"Hello {name},\n\nYour {role} account has been created.\n"
                f"Login ID: {login_id}\nTemporary Password: {temporary_password}\n\n"
                "Please log in and change your password immediately.\n"
            )
            # Every interpolated value is user-supplied: escape it for HTML.
            e = html.escape
            html_body = (
                f"<p>Hello <strong>{e(name)}</strong>,</p>"
                f"<p>Your {e(role)} account has been created.</p>"
                f"<p><strong>Login ID:</strong> {e(login_id)}<br>"
                f"<strong>Temporary Password:</strong> {e(temporary_password)}</p>"
                "<p>Please log in and change your password immediately.</p>"
            )
            sent = email_service._send_email(email, subject, html_body, text_body)
            results["email"] = {
                "sent": bool(sent),
                "detail": "Email dispatched." if sent else "Email saved to outbox (SMTP not configured).",
            }

    any_sent = any(r.get("sent") for r in results.values())
    return jsonify({
        "success": any_sent,
        "data": {"results": results},
        "message": "Credentials dispatched." if any_sent
                   else "No channel delivered. Check SMS/SMTP configuration.",
    }), (200 if any_sent else 502)


# -------------------------------------------------------------
# 1. Notices / Announcements
# -------------------------------------------------------------

@communications_bp.route("/notices", methods=["GET"])
def list_notices():
    institution_id = get_current_tenant_id()
    query = Notice.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    target = request.args.get("target")
    if target:
        query = query.filter((Notice.target_audience == target.upper()) | (Notice.target_audience == "ALL"))

    notices = query.order_by(Notice.publish_date.desc()).all()
    data = [
        {
            "id": str(n.id),
            "title": n.title,
            "content": n.content,
            "priority": n.priority,
            "target_audience": n.target_audience,
            "publish_date": n.publish_date.isoformat(),
            "expiry_date": n.expiry_date.isoformat() if n.expiry_date else None,
            "attachment_url": n.attachment_url,
            "author_name": f"{n.author.first_name} {n.author.last_name}".strip() if n.author else "Admin",
        }
        for n in notices
    ]
    return jsonify({"success": True, "data": data}), 200


@communications_bp.route("/notices", methods=["POST"])
def create_notice():
    institution_id = get_current_tenant_id(required=True)
    payload = _json_body()

    title = _str_field(payload, "title", max_len=255)
    content = _str_field(payload, "content")
    if not title or not content:
        raise APIException("title and content are required", status_code=400)

    author_id = _actor_id()

    notice = Notice(
        institution_id=institution_id,
        author_user_id=author_id,
        title=title,
        content=content,
        priority=_str_field(payload, "priority", default="NORMAL", max_len=20).upper(),
        target_audience=_str_field(payload, "target_audience", default="ALL", max_len=50).upper(),
        publish_date=date.today(),
        attachment_url=_str_field(payload, "attachment_url", default=None, max_len=500),
    )
    db.session.add(notice)
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {"id": str(notice.id), "title": notice.title},
        "message": "Notice published successfully."
    }), 201


# -------------------------------------------------------------
# 2. Messages (1-to-1)
# -------------------------------------------------------------

@communications_bp.route("/messages", methods=["GET"])
def list_messages():
    institution_id = get_current_tenant_id()
    query = Message.query
    if institution_id:
        query = query.filter_by(institution_id=institution_id)

    own_id = _actor_id()
    requested = request.args.get("user_id")
    u_uuid = _uuid(requested, "user_id") if requested else own_id
    if u_uuid != own_id:
        roles = set((get_jwt() or {}).get("roles") or [])
        if not roles & MAILBOX_ADMIN_ROLES:
            raise APIException("You may only read your own messages", status_code=403, error_code="FORBIDDEN")
    query = query.filter((Message.sender_id == u_uuid) | (Message.recipient_id == u_uuid))

    messages = query.order_by(Message.sent_at.desc()).all()
    data = [
        {
            "id": str(m.id),
            "sender_id": str(m.sender_id),
            "sender_name": f"{m.sender.first_name} {m.sender.last_name}".strip() if m.sender else None,
            "recipient_id": str(m.recipient_id),
            "recipient_name": f"{m.recipient.first_name} {m.recipient.last_name}".strip() if m.recipient else None,
            "subject": m.subject,
            "body": m.body,
            "sent_at": m.sent_at.isoformat(),
            "is_read": m.is_read,
        }
        for m in messages
    ]
    return jsonify({"success": True, "data": data}), 200


@communications_bp.route("/messages", methods=["POST"])
def send_message():
    institution_id = get_current_tenant_id(required=True)
    payload = _json_body()

    recipient_id = payload.get("recipient_id")
    body = _str_field(payload, "body", "content")

    if not recipient_id or not body:
        raise APIException("recipient_id and body are required", status_code=400)

    recipient_uuid = _uuid(recipient_id, "recipient_id")
    recipient = User.query.filter(
        User.id == recipient_uuid,
        or_(User.institution_id == institution_id, User.institution_id.is_(None)),
    ).first()
    if not recipient:
        raise APIException("Recipient not found", status_code=404)

    sender_id = _actor_id()

    msg = Message(
        institution_id=institution_id,
        sender_id=sender_id,
        recipient_id=recipient.id,
        subject=_str_field(payload, "subject", default=None, max_len=255),
        body=body,
        sent_at=datetime.now(timezone.utc),
        is_read=False,
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({
        "success": True,
        "data": {"id": str(msg.id), "sent_at": msg.sent_at.isoformat()},
        "message": "Message delivered successfully."
    }), 201

