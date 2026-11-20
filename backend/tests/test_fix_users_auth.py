"""Regression tests for user-management authorization and session-revocation fixes."""
import re
import uuid

import pytest

from app.extensions import db
from app.models import Institution, Role, User, UserRole
from app.utils.security import hash_password
from tests._auth import bearer_for

PRINCIPAL = "principal@stxaviers.edu.in"
SUPERADMIN = "superadmin@sms.edu.in"
DEMO_PW = "SchoolERP@Demo2026!"


def _login(client, identifier, password=DEMO_PW):
    res = client.post("/api/v1/auth/login", json={"email": identifier, "password": password})
    assert res.status_code == 200, res.get_json()
    data = res.get_json()["data"]
    refresh = re.search(r"refresh_token_cookie=([^;]+)", res.headers["Set-Cookie"]).group(1)
    return {"Authorization": f"Bearer {data['access_token']}"}, refresh


def _user_id(app, email):
    with app.app_context():
        return str(User.query.filter_by(email=email).first().id)


def _create_user(client, headers, role="teacher", **extra):
    suffix = uuid.uuid4().hex[:8]
    body = {
        "first_name": "Temp",
        "last_name": f"User{suffix}",
        "email": f"temp.{suffix}@stxaviers.edu.in",
        "role": role,
    }
    body.update(extra)
    res = client.post("/api/v1/users", json=body, headers=headers)
    assert res.status_code == 201, res.get_json()
    return res.get_json()["data"]


@pytest.fixture(scope="module")
def other_school(app):
    """A second institution with its own school admin and teacher."""
    with app.app_context():
        inst = Institution.query.filter_by(code="FIX-USERS-B").first()
        if not inst:
            inst = Institution(name="Fix Users Campus B", code="FIX-USERS-B",
                               email="office@fixb.example.in", board_affiliation="CBSE",
                               currency="INR", status="ACTIVE")
            db.session.add(inst)
            db.session.flush()
            for username, email, role in (
                ("fixb_admin", "admin@fixb.example.in", "school_admin"),
                ("fixb_teacher", "teacher@fixb.example.in", "teacher"),
            ):
                u = User(institution_id=inst.id, username=username, email=email,
                         password_hash=hash_password(DEMO_PW), first_name="Fix",
                         last_name="B", is_active=True, is_verified=True)
                db.session.add(u)
                db.session.flush()
                db.session.add(UserRole(user_id=u.id, role_id=Role.query.filter_by(name=role).first().id))
            db.session.commit()
        return {
            "inst_id": str(inst.id),
            "admin_email": "admin@fixb.example.in",
            "teacher_id": str(User.query.filter_by(email="teacher@fixb.example.in").first().id),
        }


# --- Finding 1: role allow-list / privilege escalation -----------------------

@pytest.mark.parametrize("role,status", [("super_admin", 403), ("school_admin", 403), ("wizard", 400)])
def test_school_admin_cannot_create_privileged_or_unknown_roles(app, client, role, status):
    with app.app_context():
        headers = bearer_for(PRINCIPAL)
    suffix = uuid.uuid4().hex[:8]
    res = client.post("/api/v1/users", json={
        "first_name": "Evil", "last_name": "Admin", "email": f"evil.{suffix}@stxaviers.edu.in", "role": role,
    }, headers=headers)
    assert res.status_code == status
    with app.app_context():
        assert User.query.filter_by(email=f"evil.{suffix}@stxaviers.edu.in").first() is None


def test_super_admin_can_create_school_admin(app, client):
    with app.app_context():
        headers = bearer_for(SUPERADMIN)
        inst_id = str(User.query.filter_by(email=PRINCIPAL).first().institution_id)
    created = _create_user(client, {**headers, "X-Institution-Id": inst_id}, role="school_admin")
    assert created["roles"] == ["school_admin"]


# --- Findings 2 & 3: tenant checks on reset / toggle / resend -----------------

def test_cross_tenant_reset_and_toggle_forbidden(app, client, other_school):
    with app.app_context():
        headers_b = bearer_for(other_school["admin_email"])
    principal_id = _user_id(app, PRINCIPAL)
    assert client.post(f"/api/v1/users/{principal_id}/reset-password", headers=headers_b).status_code == 404
    assert client.post(f"/api/v1/users/{principal_id}/toggle-status", headers=headers_b).status_code == 404
    with app.app_context():
        assert User.query.filter_by(email=PRINCIPAL).first().is_active is True


def test_principal_cannot_toggle_super_admin_or_self(app, client):
    with app.app_context():
        headers = bearer_for(PRINCIPAL)
    super_id = _user_id(app, SUPERADMIN)
    principal_id = _user_id(app, PRINCIPAL)
    assert client.post(f"/api/v1/users/{super_id}/toggle-status", headers=headers).status_code in (403, 404)
    assert client.post(f"/api/v1/users/{super_id}/reset-password", headers=headers).status_code in (403, 404)
    assert client.post(f"/api/v1/users/{principal_id}/toggle-status", headers=headers).status_code == 400
    with app.app_context():
        assert User.query.filter_by(email=SUPERADMIN).first().is_active is True
        assert User.query.filter_by(email=PRINCIPAL).first().is_active is True


def test_non_uuid_user_id_is_404(app, client):
    with app.app_context():
        headers = bearer_for(PRINCIPAL)
    for action in ("reset-password", "toggle-status", "resend-credentials"):
        assert client.post(f"/api/v1/users/not-a-uuid/{action}", headers=headers).status_code == 404


@pytest.mark.parametrize("email", ["priya.nair@stxaviers.edu.in", "vikram.patel@gmail.com", "orders@apexsupplies.in"])
def test_resend_credentials_requires_admin(app, client, email):
    with app.app_context():
        headers = bearer_for(email)
    target = _user_id(app, PRINCIPAL)
    res = client.post(f"/api/v1/users/{target}/resend-credentials", json={"channel": "sms"}, headers=headers)
    assert res.status_code == 403


def test_resend_credentials_is_tenant_scoped(app, client, other_school):
    with app.app_context():
        headers = bearer_for(PRINCIPAL)
    teacher_id = _user_id(app, "priya.nair@stxaviers.edu.in")
    assert client.post(f"/api/v1/users/{teacher_id}/resend-credentials", json={"channel": "sms"},
                       headers=headers).status_code == 200
    assert client.post(f"/api/v1/users/{other_school['teacher_id']}/resend-credentials",
                       json={"channel": "sms"}, headers=headers).status_code == 404


# --- Finding 4: list scoping -------------------------------------------------

def test_user_list_scoping(app, client, other_school):
    with app.app_context():
        principal_headers = bearer_for(PRINCIPAL)
        super_headers = bearer_for(SUPERADMIN)
    emails = {u["email"] for u in client.get("/api/v1/users", headers=principal_headers).get_json()["data"]}
    assert PRINCIPAL in emails
    assert SUPERADMIN not in emails
    assert other_school["admin_email"] not in emails

    all_emails = {u["email"] for u in client.get("/api/v1/users", headers=super_headers).get_json()["data"]}
    assert {PRINCIPAL, other_school["admin_email"], SUPERADMIN} <= all_emails

    scoped = client.get("/api/v1/users", headers={**super_headers, "X-Institution-Id": other_school["inst_id"]})
    scoped_emails = {u["email"] for u in scoped.get_json()["data"]}
    assert other_school["admin_email"] in scoped_emails
    assert PRINCIPAL not in scoped_emails


# --- Finding 5: deactivation / admin reset revoke sessions --------------------

def test_deactivation_and_admin_reset_revoke_sessions(app, client):
    with app.app_context():
        admin = bearer_for(PRINCIPAL)
    created = _create_user(client, admin)
    user_headers, _ = _login(client, created["email"], created["temporary_password"])
    assert client.get("/api/v1/auth/me", headers=user_headers).status_code == 200

    res = client.post(f"/api/v1/users/{created['id']}/toggle-status", headers=admin)
    assert res.get_json()["data"]["is_active"] is False
    assert client.get("/api/v1/auth/me", headers=user_headers).status_code == 401

    res = client.post(f"/api/v1/users/{created['id']}/toggle-status", headers=admin)
    assert res.get_json()["data"]["is_active"] is True
    user_headers, _ = _login(client, created["email"], created["temporary_password"])
    assert client.get("/api/v1/auth/me", headers=user_headers).status_code == 200

    reset = client.post(f"/api/v1/users/{created['id']}/reset-password", headers=admin)
    assert reset.status_code == 200
    assert client.get("/api/v1/auth/me", headers=user_headers).status_code == 401
    new_headers, _ = _login(client, created["email"], reset.get_json()["data"]["temporary_password"])
    assert client.get("/api/v1/auth/me", headers=new_headers).status_code == 200


# --- Findings 6 & 7: global revocation vs refresh rotation and same-second login

def test_logout_all_revokes_refresh_token_but_not_next_login(app, client):
    with app.app_context():
        admin = bearer_for(PRINCIPAL)
    created = _create_user(client, admin)
    pw = created["temporary_password"]

    headers, refresh = _login(client, created["email"], pw)
    assert client.post("/api/v1/auth/logout-all", headers=headers).status_code == 200
    # A login immediately after logout-all (typically the same second) works.
    fresh_headers, fresh_refresh = _login(client, created["email"], pw)
    assert client.get("/api/v1/auth/me", headers=fresh_headers).status_code == 200
    # The old tokens are dead, including the old refresh token.
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    res = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "TOKEN_REVOKED"
    # The fresh refresh token still rotates.
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": fresh_refresh}).status_code == 200


def test_change_password_revokes_old_refresh_token(app, client):
    with app.app_context():
        admin = bearer_for(PRINCIPAL)
    created = _create_user(client, admin)
    headers, refresh = _login(client, created["email"], created["temporary_password"])
    res = client.post("/api/v1/auth/change-password", json={
        "current_password": created["temporary_password"],
        "new_password": "Fresh#Pass2026!x",
        "confirm_password": "Fresh#Pass2026!x",
    }, headers=headers)
    assert res.status_code == 200, res.get_json()
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": refresh}).status_code == 401


# --- Finding 8: create validation --------------------------------------------

@pytest.mark.parametrize("body,status", [
    ({"login_id": "Principal"}, 409),           # generated email collides with principal@...
    ({"login_id": "PRINCIPAL", "email": "x.{s}@stxaviers.edu.in"}, 409),  # case-insensitive username
    ({"email": "not-an-email"}, 400),
    ({"first_name": "A" * 500}, 400),
    ({"first_name": 12345}, 400),
    ({"phone": "+91 98450 12345"}, 201),        # shared phone (families share numbers)
    ({"phone": "9" * 80}, 400),
])
def test_create_user_validation(app, client, body, status):
    with app.app_context():
        headers = bearer_for(PRINCIPAL)
    s = uuid.uuid4().hex[:8]
    payload = {"first_name": "Val", "last_name": "Check", "email": f"val.{s}@stxaviers.edu.in", "role": "teacher"}
    payload.update({k: (v.format(s=s) if isinstance(v, str) else v) for k, v in body.items()})
    if body.get("login_id") == "Principal":
        payload.pop("email")
    res = client.post("/api/v1/users", json=payload, headers=headers)
    assert res.status_code == status, res.get_json()


# --- Finding 9: PUT /auth/me validation --------------------------------------

def test_update_profile_validation(app, client):
    with app.app_context():
        admin = bearer_for(PRINCIPAL)
    created = _create_user(client, admin)
    headers, _ = _login(client, created["email"], created["temporary_password"])
    for body in ({"phone": 9876543210}, {"first_name": 42}, {"phone": "9" * 80}):
        assert client.put("/api/v1/auth/me", json=body, headers=headers).status_code == 400
    # A phone already used by another account is allowed (shared family numbers).
    res = client.put("/api/v1/auth/me", json={"phone": "+91 98450 12345"}, headers=headers)
    assert res.status_code == 200
    res = client.put("/api/v1/auth/me", json={"phone": "+91 90000 1" + str(uuid.uuid4().int)[:4],
                                               "first_name": "Renamed"}, headers=headers)
    assert res.status_code == 200
    assert res.get_json()["data"]["first_name"] == "Renamed"


# --- Finding 10: demo-password fallback is opt-in only -----------------------

def test_demo_password_fallback_requires_explicit_opt_in(app, client, monkeypatch):
    from app.services.auth_service import _demo_passwords_allowed
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("ALLOW_DEMO_PASSWORDS", raising=False)
    original = (app.config.get("TESTING"), app.config.get("DEBUG"))
    app.config.update(TESTING=False, DEBUG=True)
    try:
        with app.app_context():
            assert _demo_passwords_allowed() is False
        res = client.post("/api/v1/auth/login", json={"email": PRINCIPAL, "password": "admin123"})
        assert res.status_code == 401
        # The account's real password still works.
        assert client.post("/api/v1/auth/login",
                           json={"email": PRINCIPAL, "password": "Principal#2026!"}).status_code == 200
        monkeypatch.setenv("ALLOW_DEMO_PASSWORDS", "true")
        with app.app_context():
            assert _demo_passwords_allowed() is True
    finally:
        app.config.update(TESTING=original[0], DEBUG=original[1])
