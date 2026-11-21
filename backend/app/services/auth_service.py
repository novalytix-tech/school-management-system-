import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple
from sqlalchemy import func
from sqlalchemy.orm import selectinload, joinedload
from flask import current_app
from flask_jwt_extended import create_access_token, create_refresh_token
from app.extensions import db
from app.models import (
    User, Role, UserRole, RolePermission,
    TokenBlocklist, PasswordReset, AuditLog,
)
from app.utils.errors import APIException
from app.utils.security import (
    hash_password, verify_password, check_needs_rehash,
    validate_password_policy, generate_secure_token, hash_token
)
from app.services.email_service import email_service
from app.services.security_audit import record_security_event, recent_failed_logins


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Shared demo credentials accepted by the demo-password fallback (see
# _demo_passwords_allowed). Never valid unless that fallback is enabled.
DEMO_PASSWORDS = frozenset({
    "SchoolERP@Demo2026!", "shiva@123",
    "admin123", "teacher123", "student123", "parent123", "superadmin123", "supplier123",
    "Principal#2026!", "Teacher#Ananya26!", "Teacher#Sunita26!", "Teacher#Rajesh26!", "Teacher#Priya26!",
    "SuperAdmin#2026!", "Parent#2026!", "Supplier#2026!",
})


def _demo_passwords_allowed() -> bool:
    """Whether the shared demo-password fallback may be used.

    This used to switch on whenever DEBUG was set or FLASK_ENV was
    development/testing. backend/.env ships FLASK_ENV=development, so a host
    started with that file accepted "admin123" & co. as a master password for
    every non-student account. The fallback is now strictly opt-in:

    * ALLOW_DEMO_PASSWORDS=true (env var or app config) for demo/preview hosts;
    * the TESTING config, because the test-suite logs seeded staff in with the
      shared demo password rather than each account's own seed password.

    DEBUG and FLASK_ENV no longer enable it.
    """
    if current_app.config.get("TESTING"):
        return True
    flag = current_app.config.get("ALLOW_DEMO_PASSWORDS")
    if flag is None:
        flag = os.getenv("ALLOW_DEMO_PASSWORDS", "")
    if isinstance(flag, bool):
        return flag
    return str(flag).strip().lower() in ("1", "true", "yes", "on")


def _issued_at_claims() -> Dict[str, Any]:
    """Sub-second issue time carried in tokens we mint.

    The standard 'iat' claim has whole-second granularity, so it cannot order a
    token against a global revocation made within the same second. 'iat_us'
    (microseconds since the epoch, signed with the token) can.
    """
    now = datetime.now(timezone.utc)
    return {"iat_us": (now - _EPOCH) // timedelta(microseconds=1)}


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]*$")


def _field_error(message: str, field: str) -> APIException:
    return APIException(message=message, status_code=400, error_code="VALIDATION_ERROR",
                        details={"field": field})


def clean_text_field(value, field: str, max_len: int, required: bool = False) -> Optional[str]:
    """Validate an optional/required free-text field: must be a string within max_len."""
    if value is None or value == "":
        if required:
            raise _field_error(f"{field} is required.", field)
        return None
    if not isinstance(value, str):
        raise _field_error(f"{field} must be a string.", field)
    cleaned = value.strip()
    if required and not cleaned:
        raise _field_error(f"{field} is required.", field)
    if len(cleaned) > max_len:
        raise _field_error(f"{field} must be at most {max_len} characters.", field)
    return cleaned or None


def clean_email(value, field: str = "email") -> Optional[str]:
    email = clean_text_field(value, field, 255)
    if email is None:
        return None
    email = email.lower()
    if not _EMAIL_RE.match(email):
        raise _field_error("Enter a valid email address.", field)
    return email


def clean_phone(value, field: str = "phone") -> Optional[str]:
    """Validate a phone number (also a login identifier): 7-15 digits, phone punctuation only."""
    phone = clean_text_field(value, field, 32)
    if phone is None:
        return None
    digits = re.sub(r"[^\d]", "", phone)
    if not _PHONE_RE.match(phone) or not 7 <= len(digits) <= 15:
        raise _field_error("Enter a valid phone number.", field)
    return phone


class AuthService:
    """Enterprise authentication and identity lifecycle management."""

    @staticmethod
    def is_globally_revoked(user_id: uuid.UUID, jwt_payload: Dict[str, Any]) -> bool:
        """True when a user-wide ('all') revocation postdates this token's issue time.

        Tokens minted by this service carry 'iat_us', so a token issued before a
        logout-all/password change is revoked while a login made right after it,
        even within the same second, is not. Tokens without 'iat_us' fall back to
        the whole-second 'iat' and are treated conservatively: a revocation in the
        same second as issue revokes them.
        """
        iat_us = jwt_payload.get("iat_us")
        if isinstance(iat_us, int) and not isinstance(iat_us, bool):
            issued_at = _EPOCH + timedelta(microseconds=iat_us)
        else:
            iat = jwt_payload.get("iat")
            if not isinstance(iat, (int, float)) or isinstance(iat, bool):
                # No usable issue time: any global revocation applies.
                issued_at = None
            else:
                issued_at = datetime.fromtimestamp(int(iat), timezone.utc)

        query = TokenBlocklist.query.filter(
            TokenBlocklist.user_id == user_id,
            TokenBlocklist.token_type == "all",
        )
        if issued_at is not None:
            query = query.filter(TokenBlocklist.revoked_at >= issued_at)
        return db.session.query(query.exists()).scalar()

    @staticmethod
    def _best_identifier_match(candidates, identifier: str, resolved: str, digits: str):
        """Pick the account the identifier most specifically names.

        The lookup ORs exact email/username, alias and phone matches, so more than
        one user can match (e.g. an alias email and the account it aliases). An
        exact match on what was typed must always win, otherwise a login could
        resolve to a different account than the one requested.
        """
        def rank(u):
            email = (u.email or "").lower()
            username = (u.username or "").lower()
            if email == identifier:
                return 0
            if username == identifier:
                return 1
            if email == resolved:
                return 2
            if username == resolved:
                return 3
            phone_digits = re.sub(r"[^\d]", "", u.phone or "")
            if "@" not in identifier and len(digits) >= 7 and phone_digits.endswith(digits[-10:]):
                return 4
            return 5

        ranked = sorted(candidates, key=rank)
        if not ranked or rank(ranked[0]) == 5:
            return None
        return ranked[0]

    @staticmethod
    def authenticate_user(
        email: str,
        password: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> Dict[str, Any]:
        """Validate credentials, check tenant and account status, and issue JWT tokens."""
        if not email or not password:
            raise APIException(
                message="Email and password are required.",
                status_code=400,
                error_code="CREDENTIALS_REQUIRED"
            )

        # Lookup user by case-insensitive email or username with alias support
        clean_identifier = email.strip().lower()
        ALIAS_MAP = {
            "orders@apexsupplies.in": "vendor@apexsupplies.com",
            "orders@apexsupplies.com": "vendor@apexsupplies.com",
            "vendor@apexsupplies.in": "vendor@apexsupplies.com",
            "admin@stxaviers.edu.in": "principal@stxaviers.edu.in",
            "rajesh.sharma.parent@gmail.com": "vikram.patel@gmail.com",
        }
        resolved_identifier = ALIAS_MAP.get(clean_identifier, clean_identifier)
        digits = re.sub(r"[^\d]", "", clean_identifier)

        filter_condition = (
            (func.lower(User.email) == clean_identifier) |
            (func.lower(User.username) == clean_identifier) |
            (func.lower(User.email) == resolved_identifier) |
            (func.lower(User.username) == resolved_identifier)
        )
        # Phone login only applies to phone-shaped identifiers; an email or
        # username that merely contains digits must never match someone's phone.
        is_phone_identifier = "@" not in clean_identifier and len(digits) >= 7
        if is_phone_identifier:
            last10 = digits[-10:]
            filter_condition = filter_condition | (User.phone.like(f"%{last10}%"))

        # Eager-load the full identity graph (roles → permissions) and the
        # institution in a handful of batched queries. Without this, role_names
        # and permissions below trigger a cascade of lazy loads (one query per
        # role, per role-permission and per permission). Over a cross-region DB
        # link (e.g. Vercel/Render → Supabase Mumbai) each round-trip is ~150ms,
        # so that N+1 turns login into a multi-second request.
        candidates = (
            User.query
            .options(
                selectinload(User.user_roles)
                .selectinload(UserRole.role)
                .selectinload(Role.role_permissions)
                .selectinload(RolePermission.permission),
                joinedload(User.institution),
            )
            .filter(filter_condition)
            # Soft-deleted accounts must never resolve: a student removed by an
            # admin cannot authenticate, even if a stale users row lingers.
            .filter(User.is_deleted.is_(False))
            .limit(10)
            .all()
        )
        user = AuthService._best_identifier_match(
            candidates, clean_identifier, resolved_identifier, digits
        )

        # Generic error message to prevent account enumeration
        generic_error = "Invalid email or password."
        if not user:
            raise APIException(
                message=generic_error,
                status_code=401,
                error_code="INVALID_CREDENTIALS"
            )

        # Account protection: after too many failed attempts the account is
        # locked for a while, and even the right password is refused until it
        # expires (so guessing can't continue). Checked before verification.
        max_failed = int(current_app.config.get("LOGIN_MAX_FAILED_ATTEMPTS", 5))
        lock_minutes = int(current_app.config.get("LOGIN_LOCKOUT_MINUTES", 15))
        failures = recent_failed_logins(user.id, lock_minutes)
        if len(failures) >= max_failed:
            unlock_at = failures[-1] + timedelta(minutes=lock_minutes)
            remaining = max(1, int((unlock_at - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
            raise APIException(
                message=f"Too many failed sign-in attempts. Try again in {remaining} minute(s) or reset your password.",
                status_code=429,
                error_code="ACCOUNT_LOCKED",
            )

        # Verify password using Argon2id
        is_valid_pw = verify_password(password, user.password_hash)
        if not is_valid_pw:
            # Demo-password fallback: explicit opt-in only (see _demo_passwords_allowed).
            if _demo_passwords_allowed() and password in DEMO_PASSWORDS:
                if "student" not in user.role_names or password != "SchoolERP@Demo2026!":
                    is_valid_pw = True

        if not is_valid_pw:
            # Audit the failure (never the password) and lock on the threshold.
            record_security_event(
                "LOGIN_FAILED", user.id, institution_id=user.institution_id,
                summary="Failed sign-in attempt (wrong password).",
            )
            if len(failures) + 1 >= max_failed:
                record_security_event(
                    "ACCOUNT_LOCKED", user.id, institution_id=user.institution_id,
                    summary=f"Locked for {lock_minutes} minutes after {max_failed} failed sign-in attempts.",
                )
            db.session.commit()
            raise APIException(
                message=generic_error,
                status_code=401,
                error_code="INVALID_CREDENTIALS"
            )

        # Check account activation status
        if not user.is_active:
            raise APIException(
                message="This account has been deactivated. Please contact school administration.",
                status_code=403,
                error_code="ACCOUNT_INACTIVE"
            )

        # Check account verification status
        if not user.is_verified:
            raise APIException(
                message="Account email verification is pending.",
                status_code=401,
                error_code="ACCOUNT_NOT_VERIFIED"
            )

        # Defence in depth against "ghost logins": reject access when the linked
        # student profile has been withdrawn or soft-deleted, even if the users
        # row itself was somehow left active (e.g. legacy data). The is_deleted
        # lookup filter above already blocks properly-deleted accounts. Only the
        # student role can have a student profile, so this extra round-trip is
        # skipped for every other role (admins, teachers, parents, suppliers).
        linked_student = None
        if "student" in user.role_names:
            from app.models import Student
            linked_student = Student.query.filter_by(user_id=user.id).first()
            if linked_student and (linked_student.is_deleted or linked_student.status == "WITHDRAWN"):
                raise APIException(
                    message="This account is no longer active. Please contact school administration.",
                    status_code=403,
                    error_code="ACCOUNT_REVOKED"
                )

        # Check institution status if user belongs to one. The institution is
        # already eager-loaded above, so reuse it instead of issuing another
        # cross-region query on every login.
        if user.institution_id:
            inst = user.institution
            if inst and inst.status != "ACTIVE":
                raise APIException(
                    message="Institution access is currently suspended or inactive. Please contact support.",
                    status_code=403,
                    error_code="INSTITUTION_SUSPENDED"
                )

        # Rehash check: upgrade Argon2 hash if cost parameters changed
        if check_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        # Generate unique token identifiers (JTIs)
        access_jti = str(uuid.uuid4())
        refresh_jti = str(uuid.uuid4())

        roles = user.role_names
        permissions = user.permissions
        inst_id_str = str(user.institution_id) if user.institution_id else None
        issued_claims = _issued_at_claims()

        # Build access token claims (15 minutes lifespan)
        access_claims = {
            "institution_id": inst_id_str,
            "roles": roles,
            "permissions": permissions,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "type": "access",
            "jti": access_jti,
            **issued_claims,
        }
        access_token = create_access_token(
            identity=str(user.id),
            additional_claims=access_claims
        )

        # Build refresh token claims (7 days lifespan)
        refresh_claims = {
            "type": "refresh",
            "jti": refresh_jti,
            "institution_id": inst_id_str,
            **issued_claims,
        }
        refresh_token = create_refresh_token(
            identity=str(user.id),
            additional_claims=refresh_claims
        )

        # Build the response payload from the already-loaded identity graph BEFORE
        # committing. expire_on_commit would otherwise expire the eager-loaded user
        # and institution, forcing two extra SELECTs just to serialize the response.
        inst_data = None
        if user.institution:
            inst_data = {
                "id": str(user.institution.id),
                "code": user.institution.code,
                "name": user.institution.name,
                "subdomain": user.institution.subdomain,
                "board_affiliation": user.institution.board_affiliation,
                "currency": user.institution.currency,
                "status": user.institution.status,
            }
        # The id of the record this account represents in /sync/store (student id,
        # teacher user id, parent id), so the client resolves its own profile
        # instead of guessing one.
        reference_id = None
        if linked_student is not None:
            reference_id = str(linked_student.id)
        elif "teacher" in roles:
            reference_id = str(user.id)
        elif "parent" in roles:
            from app.models import Parent
            parent = Parent.query.filter_by(user_id=user.id).first()
            reference_id = str(parent.id) if parent else None

        user_payload = {
            "id": str(user.id),
            "reference_id": reference_id,
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "full_name": user.full_name,
            "phone": user.phone,
            "avatar_url": user.avatar_url,
            "roles": roles,
            "permissions": permissions,
            "institution_id": inst_id_str,
            "institution": inst_data,
            "is_active": user.is_active,
            "is_verified": user.is_verified,
            "must_change_password": getattr(user, "must_change_password", False),
        }

        # Update last login timestamp
        user.last_login_at = datetime.now(timezone.utc)

        # Record audit log
        audit = AuditLog(
            institution_id=user.institution_id,
            actor_user_id=user.id,
            actor_role=",".join(roles) if roles else None,
            entity_type="User",
            entity_id=user.id,
            action="LOGIN",
            diff_summary="User authenticated successfully.",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.session.add(audit)
        db.session.commit()

        return {
            "user": user_payload,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": 900,  # 15 minutes
        }

    @staticmethod
    def rotate_refresh_token(
        refresh_payload: Dict[str, Any],
        ip_address: Optional[str] = None
    ) -> Tuple[str, str]:
        """Rotate refresh token: revoke current JTI and issue a fresh access & refresh token pair."""
        old_jti = refresh_payload.get("jti")
        user_id_str = refresh_payload.get("sub")

        if not old_jti or not user_id_str:
            raise APIException(
                message="Malformed refresh token claims.",
                status_code=401,
                error_code="INVALID_REFRESH_TOKEN"
            )

        # Check for Token Reuse Attack
        is_revoked = TokenBlocklist.query.filter_by(jti=old_jti).first()
        if is_revoked:
            # Token reuse detected! Invalidate all sessions for this user immediately.
            AuthService.logout_all_sessions(uuid.UUID(user_id_str))

            audit = AuditLog(
                actor_user_id=uuid.UUID(user_id_str),
                entity_type="TokenBlocklist",
                entity_id=uuid.UUID(user_id_str),
                action="TOKEN_REUSE_DETECTED",
                diff_summary=f"Revoked token {old_jti} re-presented. All user sessions invalidated.",
                ip_address=ip_address,
            )
            db.session.add(audit)
            db.session.commit()

            raise APIException(
                message="Security alert: Token reuse detected. All active sessions have been terminated.",
                status_code=401,
                error_code="TOKEN_REUSE_DETECTED"
            )

        try:
            user_uuid = uuid.UUID(user_id_str)
        except (ValueError, TypeError, AttributeError):
            raise APIException(
                message="Malformed refresh token claims.",
                status_code=401,
                error_code="INVALID_REFRESH_TOKEN"
            )

        # A user-wide revocation (logout-all, password change/reset, account
        # deactivation) must also kill refresh tokens issued before it; decode_token
        # does not consult the blocklist loader, so check it explicitly here.
        if AuthService.is_globally_revoked(user_uuid, refresh_payload):
            raise APIException(
                message="This session has been revoked. Please authenticate again.",
                status_code=401,
                error_code="TOKEN_REVOKED"
            )

        # Fetch user
        user = db.session.get(User, user_uuid)
        if not user or not user.is_active:
            raise APIException(
                message="User account is inactive or no longer exists.",
                status_code=401,
                error_code="USER_NOT_FOUND"
            )

        # Revoke the consumed refresh token
        exp_timestamp = refresh_payload.get("exp")
        expires_at = datetime.fromtimestamp(exp_timestamp, timezone.utc) if exp_timestamp else datetime.now(timezone.utc) + timedelta(days=7)
        block_entry = TokenBlocklist(
            jti=old_jti,
            user_id=user.id,
            token_type="refresh",
            expires_at=expires_at,
        )
        db.session.add(block_entry)

        # Generate fresh token pair (Rotation)
        new_access_jti = str(uuid.uuid4())
        new_refresh_jti = str(uuid.uuid4())
        inst_id_str = str(user.institution_id) if user.institution_id else None
        issued_claims = _issued_at_claims()

        access_claims = {
            "institution_id": inst_id_str,
            "roles": user.role_names,
            "permissions": user.permissions,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "type": "access",
            "jti": new_access_jti,
            **issued_claims,
        }
        new_access_token = create_access_token(
            identity=str(user.id),
            additional_claims=access_claims
        )

        refresh_claims = {
            "type": "refresh",
            "jti": new_refresh_jti,
            "institution_id": inst_id_str,
            **issued_claims,
        }
        new_refresh_token = create_refresh_token(
            identity=str(user.id),
            additional_claims=refresh_claims
        )

        db.session.commit()
        return new_access_token, new_refresh_token

    @staticmethod
    def logout_current_session(
        access_payload: Optional[Dict[str, Any]] = None,
        refresh_payload: Optional[Dict[str, Any]] = None,
        user_id: Optional[uuid.UUID] = None
    ) -> None:
        """Revoke current session tokens into the token blocklist."""
        now = datetime.now(timezone.utc)

        if access_payload and "jti" in access_payload:
            jti = access_payload["jti"]
            uid = user_id or uuid.UUID(access_payload["sub"])
            exp = datetime.fromtimestamp(access_payload.get("exp", now.timestamp() + 900), timezone.utc)
            if not TokenBlocklist.query.filter_by(jti=jti).first():
                db.session.add(TokenBlocklist(jti=jti, user_id=uid, token_type="access", expires_at=exp))

        if refresh_payload and "jti" in refresh_payload:
            jti = refresh_payload["jti"]
            uid = user_id or uuid.UUID(refresh_payload["sub"])
            exp = datetime.fromtimestamp(refresh_payload.get("exp", now.timestamp() + 604800), timezone.utc)
            if not TokenBlocklist.query.filter_by(jti=jti).first():
                db.session.add(TokenBlocklist(jti=jti, user_id=uid, token_type="refresh", expires_at=exp))

        if user_id:
            record_security_event("LOGOUT", user_id, actor_user_id=user_id, summary="Signed out of this session.")
        db.session.commit()

    @staticmethod
    def logout_all_sessions(user_id: uuid.UUID) -> None:
        """Revoke all active sessions for a user by adding a global session invalidation record."""
        now = datetime.now(timezone.utc)
        # Create user-wide global revocation record valid for next 7 days
        global_jti = str(uuid.uuid4())
        db.session.add(TokenBlocklist(
            jti=global_jti,
            user_id=user_id,
            token_type="all",
            expires_at=now + timedelta(days=7),
            revoked_at=now,
        ))
        record_security_event("ALL_SESSIONS_REVOKED", user_id, summary="Every active session was signed out.")
        db.session.commit()

    @staticmethod
    def request_password_reset(email: str, ip_address: Optional[str] = None, frontend_url: Optional[str] = None) -> str:
        """Initiate password reset request with high-entropy cryptographic token."""
        if not email:
            raise APIException(message="Email address is required.", status_code=400, error_code="EMAIL_REQUIRED")

        user = User.query.filter(func.lower(User.email) == email.strip().lower()).first()

        # Generate generic message to prevent email enumeration
        generic_msg = "If this email address is registered, instructions to reset your password have been sent."

        if user and user.is_active:
            raw_token = generate_secure_token(32)
            token_digest = hash_token(raw_token)
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

            reset_entry = PasswordReset(
                user_id=user.id,
                token_hash=token_digest,
                expires_at=expires_at,
                is_used=False,
            )
            db.session.add(reset_entry)

            # Record audit event
            audit = AuditLog(
                institution_id=user.institution_id,
                actor_user_id=user.id,
                entity_type="PasswordReset",
                entity_id=user.id,
                action="PASSWORD_RESET_REQUESTED",
                diff_summary="Password reset token issued (30 min validity).",
                ip_address=ip_address,
            )
            db.session.add(audit)
            db.session.commit()

            # Dispatch notification email
            email_service.send_password_reset_email(user.email, user.full_name, raw_token, frontend_url)

        return generic_msg

    @staticmethod
    def reset_password(
        token: str,
        new_password: str,
        confirm_password: str,
        ip_address: Optional[str] = None
    ) -> bool:
        """Complete password reset using valid cryptographic reset token."""
        if not token:
            raise APIException(message="Reset token is required.", status_code=400, error_code="TOKEN_REQUIRED")

        if new_password != confirm_password:
            raise APIException(
                message="New password and confirmation password do not match.",
                status_code=400,
                error_code="PASSWORD_MISMATCH"
            )

        valid, error_msg = validate_password_policy(new_password)
        if not valid:
            raise APIException(
                message=error_msg,
                status_code=400,
                error_code="PASSWORD_POLICY_VIOLATION"
            )

        token_digest = hash_token(token)
        reset_entry = PasswordReset.query.filter_by(token_hash=token_digest).first()

        if not reset_entry:
            raise APIException(
                message="Invalid or expired password reset token.",
                status_code=400,
                error_code="INVALID_RESET_TOKEN"
            )

        if reset_entry.is_used:
            raise APIException(
                message="This password reset token has already been used.",
                status_code=400,
                error_code="RESET_TOKEN_USED"
            )

        now = datetime.now(timezone.utc)
        if reset_entry.expires_at < now:
            raise APIException(
                message="This password reset token has expired. Please request a new one.",
                status_code=400,
                error_code="RESET_TOKEN_EXPIRED"
            )

        # Update user's password
        user = db.session.get(User, reset_entry.user_id)
        if not user or not user.is_active:
            raise APIException(
                message="Associated user account is inactive or not found.",
                status_code=400,
                error_code="USER_NOT_FOUND"
            )

        user.password_hash = hash_password(new_password)
        reset_entry.is_used = True
        reset_entry.used_at = now

        # Invalidate all existing user sessions
        AuthService.logout_all_sessions(user.id)

        # Audit log
        audit = AuditLog(
            institution_id=user.institution_id,
            actor_user_id=user.id,
            entity_type="User",
            entity_id=user.id,
            action="PASSWORD_RESET_COMPLETED",
            diff_summary="User password reset completed. All prior sessions revoked.",
            ip_address=ip_address,
        )
        db.session.add(audit)
        db.session.commit()

        # Send confirmation email
        email_service.send_password_changed_notification(user.email, user.full_name)
        return True

    @staticmethod
    def find_user_by_identifier(identifier: str) -> Optional[Dict[str, Any]]:
        if not identifier:
            return None
        clean_id = identifier.strip().lower()
        digits = re.sub(r"[^\d]", "", clean_id)
        ALIAS_MAP = {
            "orders@apexsupplies.in": "vendor@apexsupplies.com",
            "orders@apexsupplies.com": "vendor@apexsupplies.com",
            "vendor@apexsupplies.in": "vendor@apexsupplies.com",
            "admin@stxaviers.edu.in": "principal@stxaviers.edu.in",
            "rajesh.sharma.parent@gmail.com": "vikram.patel@gmail.com",
        }
        resolved = ALIAS_MAP.get(clean_id, clean_id)
        filter_cond = (
            (func.lower(User.email) == clean_id) |
            (func.lower(User.username) == clean_id) |
            (func.lower(User.email) == resolved) |
            (func.lower(User.username) == resolved)
        )
        if len(digits) >= 7:
            filter_cond = filter_cond | (User.phone.like(f"%{digits[-10:]}%"))

        u = User.query.filter(filter_cond).first()
        if not u:
            return None
        return {
            "id": str(u.id),
            "username": u.username,
            "email": u.email,
            "phone": u.phone,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "full_name": u.full_name,
            "roles": u.role_names,
            "must_change_password": getattr(u, "must_change_password", False),
        }

    @staticmethod
    def update_user_password(user_id, new_password: str, must_change_password: bool = False) -> bool:
        uid = uuid.UUID(str(user_id)) if isinstance(user_id, str) else user_id
        u = User.query.get(uid)
        if not u:
            return False
        u.password_hash = hash_password(new_password)
        if hasattr(u, "must_change_password"):
            u.must_change_password = must_change_password
        db.session.commit()
        return True

    @staticmethod
    def change_password(
        user_id: uuid.UUID,
        current_password: str,
        new_password: str,
        confirm_password: str,
        ip_address: Optional[str] = None
    ) -> bool:
        """Change password for an authenticated user."""
        user = db.session.get(User, user_id)
        if not user or not user.is_active:
            raise APIException(message="User account not found.", status_code=404, error_code="USER_NOT_FOUND")

        is_current_valid = verify_password(current_password, user.password_hash)
        if not is_current_valid:
            # Same explicit opt-in as the login fallback (see _demo_passwords_allowed).
            if _demo_passwords_allowed() and current_password == "SchoolERP@Demo2026!":
                is_current_valid = True

        if not is_current_valid:
            raise APIException(
                message="The current password you entered is incorrect.",
                status_code=400,
                error_code="INVALID_CURRENT_PASSWORD"
            )

        if new_password != confirm_password:
            raise APIException(
                message="New password and confirmation password do not match.",
                status_code=400,
                error_code="PASSWORD_MISMATCH"
            )

        valid, error_msg = validate_password_policy(new_password)
        if not valid:
            raise APIException(message=error_msg, status_code=400, error_code="PASSWORD_POLICY_VIOLATION")

        # Prevent reuse of current password
        if verify_password(new_password, user.password_hash) or new_password == current_password:
            raise APIException(
                message="New password cannot be the same as your current password.",
                status_code=400,
                error_code="PASSWORD_REUSE_FORBIDDEN"
            )

        user.password_hash = hash_password(new_password)

        # Invalidate other sessions
        AuthService.logout_all_sessions(user.id)

        audit = AuditLog(
            institution_id=user.institution_id,
            actor_user_id=user.id,
            entity_type="User",
            entity_id=user.id,
            action="PASSWORD_CHANGED",
            diff_summary="User changed account password. Other sessions invalidated.",
            ip_address=ip_address,
        )
        db.session.add(audit)
        db.session.commit()

        email_service.send_password_changed_notification(user.email, user.full_name)
        return True

    @staticmethod
    def find_user_by_identifier(identifier: str) -> Optional[Dict[str, Any]]:
        if not identifier:
            return None
        clean_id = identifier.strip().lower()
        digits = re.sub(r"[^\d]", "", clean_id)
        ALIAS_MAP = {
            "orders@apexsupplies.in": "vendor@apexsupplies.com",
            "orders@apexsupplies.com": "vendor@apexsupplies.com",
            "vendor@apexsupplies.in": "vendor@apexsupplies.com",
            "admin@stxaviers.edu.in": "principal@stxaviers.edu.in",
            "rajesh.sharma.parent@gmail.com": "vikram.patel@gmail.com",
        }
        resolved = ALIAS_MAP.get(clean_id, clean_id)
        filter_cond = (
            (func.lower(User.email) == clean_id) |
            (func.lower(User.username) == clean_id) |
            (func.lower(User.email) == resolved) |
            (func.lower(User.username) == resolved)
        )
        if len(digits) >= 7:
            filter_cond = filter_cond | (User.phone.like(f"%{digits[-10:]}%"))

        u = User.query.filter(filter_cond).first()
        if not u:
            return None
        return {
            "id": str(u.id),
            "username": u.username,
            "email": u.email,
            "phone": u.phone,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "full_name": u.full_name,
            "roles": u.role_names,
            "must_change_password": getattr(u, "must_change_password", False),
        }

    @staticmethod
    def update_user_password(user_id, new_password: str, must_change_password: bool = False) -> bool:
        uid = uuid.UUID(str(user_id)) if isinstance(user_id, str) else user_id
        u = User.query.get(uid)
        if not u:
            return False
        u.password_hash = hash_password(new_password)
        if hasattr(u, "must_change_password"):
            u.must_change_password = must_change_password
        db.session.commit()
        return True
