"""Security hardening: lockout, CSRF on cookie refresh, headers, secrets, audit trail."""
import uuid

import pytest

from app import create_app
from app.extensions import db
from app.models import AuditLog, Role, User, UserRole
from app.utils.security import hash_password
from tests._auth import admin_headers, bearer_for

PASSWORD = "Lockout#Test2026!"


@pytest.fixture
def lockable_user(app):
    """A throwaway teacher account so lockouts never affect seeded personas."""
    with app.app_context():
        principal = User.query.filter_by(email="principal@stxaviers.edu.in").first()
        email = f"lock.{uuid.uuid4().hex[:8]}@stxaviers.edu.in"
        user = User(institution_id=principal.institution_id, username=email.split("@")[0], email=email,
                    password_hash=hash_password(PASSWORD), first_name="Lock", last_name="Test",
                    is_active=True, is_verified=True)
        db.session.add(user)
        db.session.flush()
        db.session.add(UserRole(user_id=user.id, role_id=Role.query.filter_by(name="teacher").first().id))
        db.session.commit()
        return {"id": user.id, "email": email}


def _login(client, email, password):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_account_locks_after_repeated_failures(client, app, lockable_user):
    email = lockable_user["email"]
    for _ in range(5):
        assert _login(client, email, "wrong-password").status_code == 401

    # Locked: even the correct password is refused until the lock expires.
    res = _login(client, email, PASSWORD)
    assert res.status_code == 429
    assert res.get_json()["error"]["code"] == "ACCOUNT_LOCKED"

    with app.app_context():
        actions = [a for (a,) in db.session.query(AuditLog.action).filter(AuditLog.entity_id == lockable_user["id"])]
        assert actions.count("LOGIN_FAILED") == 5
        assert "ACCOUNT_LOCKED" in actions
        # The audit trail never stores the attempted password.
        summaries = " ".join(s or "" for (s,) in db.session.query(AuditLog.diff_summary)
                             .filter(AuditLog.entity_id == lockable_user["id"]))
        assert "wrong-password" not in summaries and PASSWORD not in summaries


def test_successful_login_resets_failure_count(client, lockable_user):
    email = lockable_user["email"]
    for _ in range(4):
        _login(client, email, "wrong-password")
    assert _login(client, email, PASSWORD).status_code == 200
    for _ in range(4):
        _login(client, email, "wrong-password")
    assert _login(client, email, PASSWORD).status_code == 200


def test_cookie_refresh_requires_anti_csrf_header(client, lockable_user):
    assert _login(client, lockable_user["email"], PASSWORD).status_code == 200
    # A cross-site form/navigation carries the cookie but can't add headers.
    res = client.post("/api/v1/auth/refresh")
    assert res.status_code == 403
    assert res.get_json()["error"]["code"] == "CSRF_HEADER_REQUIRED"
    res = client.post("/api/v1/auth/refresh", headers={"X-Requested-With": "XMLHttpRequest"})
    assert res.status_code == 200


def test_refresh_error_does_not_leak_library_details(client):
    res = client.post("/api/v1/auth/refresh", json={"refresh_token": "not.a.jwt"})
    assert res.status_code == 401
    assert res.get_json()["error"]["message"] == "Invalid or expired refresh token."


def test_security_headers_on_api_responses(client):
    res = client.get("/api/v1/health")
    for header, value in {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }.items():
        assert res.headers.get(header) == value
    assert "default-src 'none'" in res.headers.get("Content-Security-Policy", "")


def test_access_tokens_are_not_accepted_from_cookies(client, app):
    with app.app_context():
        token = admin_headers()["Authorization"].split(" ", 1)[1]
    client.set_cookie("access_token_cookie", token, domain="localhost")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_refuses_to_start_with_placeholder_secrets(monkeypatch):
    from app.config import DevelopmentConfig
    monkeypatch.setattr(DevelopmentConfig, "DATABASE_ENABLED", False)
    monkeypatch.setattr(DevelopmentConfig, "JWT_SECRET_KEY", "sms-erp-default-jwt-secret-key")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        create_app("development")
    monkeypatch.setattr(DevelopmentConfig, "JWT_SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        create_app("development")


def test_school_admin_cannot_take_over_another_admin(client, app):
    with app.app_context():
        principal = User.query.filter_by(email="principal@stxaviers.edu.in").first()
        other = User(institution_id=principal.institution_id, username=f"adm{uuid.uuid4().hex[:6]}",
                     email=f"adm.{uuid.uuid4().hex[:6]}@stxaviers.edu.in", password_hash=hash_password(PASSWORD),
                     first_name="Second", last_name="Admin", is_active=True, is_verified=True)
        db.session.add(other)
        db.session.flush()
        db.session.add(UserRole(user_id=other.id, role_id=Role.query.filter_by(name="school_admin").first().id))
        db.session.commit()
        other_id = str(other.id)
        headers = admin_headers()
    assert client.post(f"/api/v1/users/{other_id}/reset-password", headers=headers).status_code == 403
    assert client.post(f"/api/v1/users/{other_id}/toggle-status", headers=headers).status_code == 403


def test_admin_actions_are_audited(client, app, lockable_user):
    with app.app_context():
        headers = admin_headers()
    uid = str(lockable_user["id"])
    assert client.post(f"/api/v1/users/{uid}/reset-password", headers=headers).status_code == 200
    with app.app_context():
        actions = {a for (a,) in db.session.query(AuditLog.action).filter(AuditLog.entity_id == lockable_user["id"])}
    assert {"ADMIN_PASSWORD_RESET", "ACCOUNT_UNLOCKED", "ALL_SESSIONS_REVOKED"} <= actions


def test_avatar_url_must_be_a_web_link(client, app, lockable_user):
    with app.app_context():
        headers = bearer_for(lockable_user["email"])
    for bad in ("javascript:alert(1)", "data:text/html,<script>x</script>", 'https://x.test/a"onerror=1'):
        assert client.put("/api/v1/auth/me", json={"avatar_url": bad}, headers=headers).status_code == 400
    assert client.put("/api/v1/auth/me", json={"avatar_url": "https://cdn.example.com/a.png"},
                      headers=headers).status_code == 200
