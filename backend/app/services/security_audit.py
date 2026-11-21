"""
Security audit trail helpers.

Every security-relevant event (failed logins, lockouts, logouts, account and
role changes, payments) is written to ``audit_logs``. Entries describe WHAT
happened and WHO did it, never secrets: no passwords, tokens, OTPs or full
card/bank details are ever passed in ``summary``.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import has_request_context, request

from app.extensions import db
from app.models import AuditLog


def _request_meta():
    if not has_request_context():
        return None, None
    agent = request.user_agent.string if request.user_agent else None
    return request.remote_addr, (agent[:255] if agent else None)


def record_security_event(
    action: str,
    entity_id: uuid.UUID,
    *,
    entity_type: str = "User",
    actor_user_id: Optional[uuid.UUID] = None,
    actor_role: Optional[str] = None,
    institution_id: Optional[uuid.UUID] = None,
    summary: Optional[str] = None,
    commit: bool = False,
) -> None:
    """Add an audit entry to the current session (and commit it if asked)."""
    ip, agent = _request_meta()
    db.session.add(AuditLog(
        institution_id=institution_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action[:50],
        diff_summary=summary,
        ip_address=ip,
        user_agent=agent,
    ))
    if commit:
        db.session.commit()


def recent_failed_logins(user_id: uuid.UUID, window_minutes: int) -> list:
    """Failed-login timestamps for a user since their last successful login, within the window."""
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    last_success = (
        db.session.query(db.func.max(AuditLog.created_at))
        .filter(AuditLog.entity_id == user_id, AuditLog.action.in_(("LOGIN", "ACCOUNT_UNLOCKED", "PASSWORD_RESET_COMPLETED", "PASSWORD_CHANGED")))
        .scalar()
    )
    if last_success and last_success > since:
        since = last_success
    return [
        ts for (ts,) in db.session.query(AuditLog.created_at)
        .filter(AuditLog.entity_id == user_id, AuditLog.action == "LOGIN_FAILED", AuditLog.created_at > since)
        .order_by(AuditLog.created_at.asc())
        .all()
    ]
