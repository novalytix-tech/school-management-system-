"""
SocketIO event handlers.

Every socket is authenticated on connect with the same JWT access token the
REST API uses (``auth={"token": ...}`` in the socket.io client, or a ``token``
query parameter / ``Authorization: Bearer`` header). The institution room is
derived from the token's ``institution_id`` claim — a client-supplied
institution id is never trusted — and no handler ever broadcasts to all
connected clients.
"""
import logging
import time
import uuid

import jwt as pyjwt
from flask import request
from flask_jwt_extended import decode_token
from flask_jwt_extended.internal_utils import verify_token_not_blocklisted
from flask_socketio import emit, join_room, disconnect

from app.extensions import socketio

logger = logging.getLogger("socketio")

# sid -> verified identity for this process's sockets.
_SOCKETS = {}

STAFF_ROLES = {"super_admin", "school_admin", "teacher"}
# Who may push each client-originated realtime event into their institution room.
EVENT_ROLES = {
    "gps_update": None,  # any authenticated non-family account (e.g. transport staff)
    "attendance_update": STAFF_ROLES,
    "notice_broadcast": STAFF_ROLES,
}
FAMILY_ROLES = {"parent", "student"}


def room_for(institution_id) -> str:
    return f"institution_{institution_id}"


def _extract_token(auth):
    token = None
    if isinstance(auth, dict):
        token = auth.get("token") or auth.get("access_token")
    if not token:
        token = request.args.get("token")
    if not token:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]
    if isinstance(token, str) and token.lower().startswith("bearer "):
        token = token[7:]
    return token.strip() if isinstance(token, str) and token.strip() else None


def authenticate_socket(auth):
    """Return verified identity for a socket token, or None if invalid/revoked."""
    token = _extract_token(auth)
    if not token:
        return None
    try:
        claims = decode_token(token)
        if claims.get("type") != "access":
            return None
        verify_token_not_blocklisted(pyjwt.get_unverified_header(token), claims)
    except Exception:
        return None
    inst = claims.get("institution_id")
    try:
        inst = str(uuid.UUID(str(inst))) if inst else None
    except ValueError:
        return None
    return {
        "user_id": claims.get("sub"),
        "institution_id": inst,
        "roles": set(claims.get("roles") or []),
        "exp": claims.get("exp"),
    }


def _identity():
    """Identity of the calling socket; disconnects it if missing or expired."""
    ident = _SOCKETS.get(request.sid)
    if not ident or (ident.get("exp") and time.time() >= ident["exp"]):
        emit("error", {"code": "UNAUTHORIZED", "message": "Socket session expired; reconnect."})
        disconnect()
        return None
    return ident


@socketio.on("connect")
def handle_connect(auth=None):
    ident = authenticate_socket(auth)
    if not ident:
        logger.info("Rejected unauthenticated socket %s", request.sid)
        return False  # refuses the connection
    _SOCKETS[request.sid] = ident
    room = None
    if ident["institution_id"]:
        room = room_for(ident["institution_id"])
        join_room(room)
    logger.info("Client connected: %s (room=%s)", request.sid, room)
    emit("connected", {"status": "connected", "sid": request.sid, "room": room})


@socketio.on("disconnect")
def handle_disconnect(*_args):
    _SOCKETS.pop(request.sid, None)
    logger.info("Client disconnected: %s", request.sid)


@socketio.on("join_institution")
def handle_join_institution(data=None):
    """Join the caller's institution room (derived from the token, not the payload)."""
    ident = _identity()
    if not ident:
        return
    requested = data.get("institution_id") if isinstance(data, dict) else None
    target = ident["institution_id"]
    if "super_admin" in ident["roles"] and not target and requested:
        # Platform admins have no home tenant and may observe any institution.
        try:
            target = str(uuid.UUID(str(requested)))
        except ValueError:
            target = None
    elif requested and str(requested) != str(target):
        emit("error", {"code": "FORBIDDEN", "message": "Cannot join another institution's room."})
        return
    if not target:
        emit("error", {"code": "TENANT_REQUIRED", "message": "No institution context for this socket."})
        return
    room = room_for(target)
    join_room(room)
    logger.info("Socket %s joined room %s", request.sid, room)
    emit("joined", {"room": room})


def _relay(event_in, event_out, data):
    ident = _identity()
    if not ident:
        return
    roles = ident["roles"]
    allowed = EVENT_ROLES.get(event_in)
    if allowed is None:
        permitted = bool(roles) and not roles <= FAMILY_ROLES
    else:
        permitted = bool(roles & allowed)
    if not permitted:
        emit("error", {"code": "FORBIDDEN_ROLE", "message": f"Not allowed to send {event_in}."})
        return
    institution_id = ident["institution_id"]
    if not institution_id:
        # Never fall back to broadcasting to every connected client.
        emit("error", {"code": "TENANT_REQUIRED", "message": "No institution context for this socket."})
        return
    payload = dict(data) if isinstance(data, dict) else {}
    payload["institution_id"] = institution_id
    emit(event_out, payload, to=room_for(institution_id))


@socketio.on("gps_update")
def handle_gps_telemetry(data=None):
    """Relay real-time bus GPS coordinates to the sender's institution room."""
    _relay("gps_update", "bus_location_update", data)


@socketio.on("attendance_update")
def handle_attendance_event(data=None):
    """Relay daily attendance events to the sender's institution room."""
    _relay("attendance_update", "attendance_recorded", data)


@socketio.on("notice_broadcast")
def handle_notice_event(data=None):
    """Relay high priority notice alerts to the sender's institution room."""
    _relay("notice_broadcast", "new_notice_published", data)
