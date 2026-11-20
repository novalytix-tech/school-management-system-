"""Token helpers for tests that call protected endpoints directly."""
import uuid

from flask_jwt_extended import create_access_token

from app.models import User


def bearer_for(email: str) -> dict:
    """Mint an access token carrying the same claims a real login issues."""
    user = User.query.filter_by(email=email).first()
    claims = {
        "institution_id": str(user.institution_id) if user.institution_id else None,
        "roles": user.role_names,
        "permissions": user.permissions,
        "type": "access",
        "jti": str(uuid.uuid4()),
    }
    token = create_access_token(identity=str(user.id), additional_claims=claims)
    return {"Authorization": f"Bearer {token}"}


def admin_headers() -> dict:
    """School admin of the seeded St. Xavier's institution."""
    return bearer_for("principal@stxaviers.edu.in")
