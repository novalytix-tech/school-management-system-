"""Realtime domain events over SocketIO.

After a server-authoritative write (student created/updated/deleted/imported),
the API emits a small ``entity_changed`` event to the writer's institution room
so every connected client — in the same browser or another — can update the
affected list live, without a manual refresh or a full re-hydrate.

Emitting is best-effort: a realtime failure must never break the underlying
write, so every emit is wrapped and swallowed with a log line.
"""
import logging

from app.extensions import socketio

logger = logging.getLogger("realtime")

ENTITY_EVENT = "entity_changed"


def emit_entity_change(institution_id, entity: str, action: str, payload: dict | None = None) -> None:
    """Broadcast an entity change to a tenant's room.

    institution_id: the tenant whose clients should hear about this.
    entity:         "student" | "class" | "guardian" | ...
    action:         "created" | "updated" | "deleted" | "imported".
    payload:        small identifying data (e.g. admission_number, id, count) the
                    client uses to patch its store directly.
    """
    if not institution_id:
        return
    room = f"institution_{institution_id}"
    body = {"entity": entity, "action": action, "data": payload or {}}
    try:
        socketio.emit(ENTITY_EVENT, body, to=room)
    except Exception:  # pragma: no cover - realtime is strictly best-effort
        logger.warning("realtime emit failed for %s/%s", entity, action, exc_info=True)
