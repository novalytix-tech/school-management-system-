from flask import Blueprint, request, jsonify
from app.extensions import db
from app.models import Institution, InstitutionSetting
from app.utils.tenant import get_current_tenant_id
from app.utils.errors import APIException
from app.utils.cache_helpers import cached_response, invalidate_on_write

institutions_bp = Blueprint("institutions", __name__, url_prefix="/institutions")

# Settings/profile change rarely but are read on every page load — cache them and
# drop the cache whenever settings are updated.
institutions_bp.after_request(invalidate_on_write("institutions"))

# Matches InstitutionSetting.setting_key String(100).
MAX_SETTING_KEY_LENGTH = 100


@institutions_bp.route("/current", methods=["GET"])
@cached_response("institutions", timeout=300)
def get_current_institution():
    institution_id = get_current_tenant_id()
    inst = Institution.query.filter_by(id=institution_id).first()
    if not inst:
        inst = Institution.query.first()

    if not inst:
        raise APIException("No institution registered", status_code=404)

    entitlements = {e.service_key: e.is_enabled for e in inst.service_entitlements}
    settings = {s.setting_key: s.setting_value for s in inst.settings}

    return jsonify({
        "success": True,
        "data": {
            "id": str(inst.id),
            "code": inst.code,
            "name": inst.name,
            "board_affiliation": inst.board_affiliation,
            "affiliation_number": inst.affiliation_number,
            "email": inst.email,
            "phone": inst.phone,
            "address": inst.address,
            "city": inst.city,
            "state": inst.state,
            "country": inst.country,
            "currency": inst.currency,
            "logo_url": inst.logo_url,
            "status": inst.status,
            "entitlements": entitlements,
            "settings": settings,
        }
    }), 200


@institutions_bp.route("/current/settings", methods=["PUT"])
def update_institution_settings():
    institution_id = get_current_tenant_id(required=True)
    inst = Institution.query.filter_by(id=institution_id).first()
    if not inst:
        raise APIException("Institution not found", status_code=404)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIException("Request body must be a JSON object of setting key/value pairs", status_code=400)
    for key in payload:
        if not key.strip():
            raise APIException("Setting keys must be non-empty", status_code=400)
        if len(key) > MAX_SETTING_KEY_LENGTH:
            raise APIException(
                f"Setting key must be at most {MAX_SETTING_KEY_LENGTH} characters", status_code=400
            )

    for key, value in payload.items():
        setting = InstitutionSetting.query.filter_by(
            institution_id=institution_id,
            setting_key=str(key)
        ).first()

        if setting:
            setting.setting_value = str(value)
        else:
            setting = InstitutionSetting(
                institution_id=institution_id,
                setting_key=str(key),
                setting_value=str(value),
            )
            db.session.add(setting)

    db.session.commit()
    return jsonify({
        "success": True,
        "message": "Institution settings updated successfully."
    }), 200

