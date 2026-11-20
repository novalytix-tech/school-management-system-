from tests._auth import admin_headers
"""
Task 6 tests: server-authoritative realtime events.
Creating / deleting / importing a student emits an ``entity_changed`` SocketIO
event to the institution room. We assert the emit call (event, payload, room)
directly — the flask-socketio test client does not simulate rooms for
server-initiated emits, so spying on socketio.emit is the reliable check.
Opt-in (needs TEST_DATABASE_URL).
"""
from datetime import datetime, timezone
from unittest.mock import patch

from app.models import Institution, Section, ClassLevel


def _tenant():
    return Institution.query.filter_by(code="STXAV-DELHI").first()


def _headers():
    return admin_headers()


def _a_section():
    inst = _tenant()
    return (
        Section.query.join(ClassLevel, Section.class_level_id == ClassLevel.id)
        .filter(ClassLevel.institution_id == inst.id)
        .first()
    )


def _entity_calls(mock_emit):
    return [c for c in mock_emit.call_args_list if c.args and c.args[0] == "entity_changed"]


def test_create_emits_entity_changed(client):
    inst = _tenant()
    section = _a_section()
    adm = f"RT-CREATE-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    with patch("app.services.realtime_service.socketio.emit") as mock_emit:
        res = client.post("/api/v1/students", headers=_headers(), json={
            "admission_number": adm, "first_name": "RT", "last_name": "Create",
            "gender": "Male", "date_of_birth": "2011-01-01",
            "email": f"{adm.lower()}@student.stxaviers.edu.in",
            "section_id": str(section.id),
        })
    assert res.status_code == 201
    calls = _entity_calls(mock_emit)
    assert len(calls) == 1
    body = calls[0].args[1]
    assert body["entity"] == "student"
    assert body["action"] == "created"
    assert body["data"]["admission_number"] == adm
    assert calls[0].kwargs.get("to") == f"institution_{inst.id}"


def test_delete_emits_entity_changed(client):
    inst = _tenant()
    section = _a_section()
    adm = f"RT-DEL-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    created = client.post("/api/v1/students", headers=_headers(), json={
        "admission_number": adm, "first_name": "RT", "last_name": "Delete",
        "gender": "Male", "date_of_birth": "2011-01-01",
        "email": f"{adm.lower()}@student.stxaviers.edu.in",
        "section_id": str(section.id),
    })
    assert created.status_code == 201
    sid = created.get_json()["data"]["id"]

    with patch("app.services.realtime_service.socketio.emit") as mock_emit:
        assert client.delete(f"/api/v1/students/{sid}", headers=_headers()).status_code == 200
    calls = _entity_calls(mock_emit)
    assert len(calls) == 1
    body = calls[0].args[1]
    assert body["entity"] == "student"
    assert body["action"] == "deleted"
    assert body["data"]["admission_number"] == adm
    assert calls[0].kwargs.get("to") == f"institution_{inst.id}"


def test_bulk_import_emits_entity_changed(client):
    inst = _tenant()
    section = _a_section()
    cls_name = section.class_level.name
    adm = f"RT-IMP-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    with patch("app.services.realtime_service.socketio.emit") as mock_emit:
        res = client.post("/api/v1/students/bulk-import", headers=_headers(), json={"students": [{
            "fullName": "RT Imported", "admissionNo": adm,
            "className": cls_name, "section": section.name,
            "gender": "Female", "dob": "2012-05-05",
        }]})
    assert res.status_code == 201
    calls = _entity_calls(mock_emit)
    assert len(calls) == 1
    body = calls[0].args[1]
    assert body["entity"] == "student"
    assert body["action"] == "imported"
    assert body["data"]["count"] == 1
    assert calls[0].kwargs.get("to") == f"institution_{inst.id}"
