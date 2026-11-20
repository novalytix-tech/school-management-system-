import uuid
from typing import Optional
from flask import request
from flask_jwt_extended import verify_jwt_in_request, get_jwt
from app.models.institution import Institution


def _requested_institution_id() -> Optional[uuid.UUID]:
    """Institution named by the 'X-Institution-Id' header or 'institution_id' query arg."""
    raw_id = request.headers.get("X-Institution-Id") or request.args.get("institution_id")
    if not raw_id:
        return None
    try:
        return uuid.UUID(str(raw_id))
    except (ValueError, TypeError):
        inst = Institution.query.filter_by(code=str(raw_id).upper()).first()
        return inst.id if inst else None


def _primary_institution_id() -> Optional[uuid.UUID]:
    """Oldest active institution (single-school deployments)."""
    inst = (
        Institution.query.filter_by(status="ACTIVE")
        .order_by(Institution.created_at.asc())
        .first()
    )
    return inst.id if inst else None


def get_current_tenant_id(required: bool = False) -> Optional[uuid.UUID]:
    """
    Resolve the tenant institution UUID for the current request.

    1. Authenticated users are pinned to the 'institution_id' in their JWT; a
       client-supplied header/query value can never move them to another tenant.
    2. A super_admin (no institution claim) may choose a tenant via the
       'X-Institution-Id' header or 'institution_id' query argument; endpoints
       that require a tenant otherwise use the primary institution.
    3. Anonymous requests (public admission forms) use the requested institution,
       else the oldest active institution (single-school deployments).
    """
    claims = None
    try:
        verify_jwt_in_request(optional=True)
        claims = get_jwt() or None
    except Exception:
        claims = None

    if claims:
        if claims.get("institution_id"):
            return uuid.UUID(str(claims["institution_id"]))
        if "super_admin" in (claims.get("roles") or []):
            requested = _requested_institution_id()
            if requested:
                return requested
            # The web client has no tenant picker yet, so a super_admin on an
            # endpoint that needs one works in the primary institution (as before).
            if required:
                primary = _primary_institution_id()
                if primary:
                    return primary
        if required:
            from app.utils.errors import APIException
            raise APIException("Active institution context required", status_code=400, error_code="TENANT_REQUIRED")
        return None

    requested = _requested_institution_id()
    if requested:
        return requested

    primary = _primary_institution_id()
    if primary:
        return primary

    if required:
        from app.utils.errors import APIException
        raise APIException("Active institution context required", status_code=400, error_code="TENANT_REQUIRED")

    return None
