"""Server-side sync of frontend store collections (/api/v1/sync/records)."""
import uuid

from app.extensions import db
from app.models import Institution, Parent, StudentParent, SyncRecord, User
from tests._auth import admin_headers, bearer_for

URL = "/api/v1/sync/records"
TEACHER = "ananya.sharma@stxaviers.edu.in"
PARENT = "vikram.patel@gmail.com"
STUDENT = "aarav.sharma@student.stxaviers.edu.in"


def _rid(prefix="rec"):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _push(client, headers, *changes):
    res = client.post(f"{URL}/batch", headers=headers, json={"changes": list(changes)})
    assert res.status_code == 200, res.get_json()
    return res.get_json()["data"]["results"]


def _pull(client, headers, since=None):
    res = client.get(URL + (f"?since={since}" if since else ""), headers=headers)
    assert res.status_code == 200, res.get_json()
    return res.get_json()["data"]


def _find(records, collection, rid):
    return next((r for r in records if r["collection"] == collection and r["id"] == rid), None)


def test_requires_authentication(client):
    assert client.get(URL).status_code == 401
    assert client.post(f"{URL}/batch", json={"changes": []}).status_code == 401


def test_create_update_delete_round_trip(client):
    h = admin_headers()
    rid = _rid("teach")
    [res] = _push(client, h, {"collection": "teachers", "id": rid, "data": {"id": rid, "fullName": "A"}})
    assert res["status"] == "ok" and res["version"] == 1

    [res] = _push(client, h, {"collection": "teachers", "id": rid, "base_version": 1,
                              "data": {"id": rid, "fullName": "B"}})
    assert res["status"] == "ok" and res["version"] == 2
    assert _find(_pull(client, h)["records"], "teachers", rid)["data"]["fullName"] == "B"

    [res] = _push(client, h, {"collection": "teachers", "id": rid, "base_version": 2, "deleted": True})
    assert res["status"] == "ok" and res["version"] == 3
    tomb = _find(_pull(client, h)["records"], "teachers", rid)
    assert tomb["deleted"] is True and tomb["data"] is None


def test_stale_write_is_a_conflict_not_an_overwrite(client):
    """Two devices edit the same record: the second (stale) save is rejected."""
    h = admin_headers()
    rid = _rid("cls")
    _push(client, h, {"collection": "classes", "id": rid, "data": {"id": rid, "name": "v1"}})
    [first] = _push(client, h, {"collection": "classes", "id": rid, "base_version": 1, "data": {"id": rid, "name": "device A"}})
    [second] = _push(client, h, {"collection": "classes", "id": rid, "base_version": 1, "data": {"id": rid, "name": "device B"}})

    assert first["status"] == "ok"
    assert second["status"] == "conflict"
    assert second["current"]["data"]["name"] == "device A"
    assert second["current"]["version"] == 2
    assert _find(_pull(client, h)["records"], "classes", rid)["data"]["name"] == "device A"


def test_creating_an_existing_id_is_a_conflict(client):
    h = admin_headers()
    rid = _rid("notice")
    _push(client, h, {"collection": "notices", "id": rid, "data": {"id": rid, "title": "first"}})
    [res] = _push(client, h, {"collection": "notices", "id": rid, "data": {"id": rid, "title": "second"}})
    assert res["status"] == "conflict"


def test_incremental_pull_returns_changes_and_tombstones(client):
    h = admin_headers()
    since = _pull(client, h)["server_time"]
    keep, gone = _rid("evt"), _rid("evt")
    _push(client, h,
          {"collection": "events", "id": keep, "data": {"id": keep}},
          {"collection": "events", "id": gone, "data": {"id": gone}})
    _push(client, h, {"collection": "events", "id": gone, "base_version": 1, "deleted": True})

    changed = _pull(client, h, since=since.replace("+", "%2B"))["records"]
    assert _find(changed, "events", keep)["deleted"] is False
    tomb = _find(changed, "events", gone)
    assert tomb["deleted"] is True and tomb["data"] is None


def test_batch_reports_invalid_changes_individually(client):
    h = admin_headers()
    rid = _rid("book")
    results = _push(client, h,
                    {"collection": "students", "id": "x", "data": {}},  # relational, excluded
                    {"collection": "books", "id": rid},                 # no data
                    "not-an-object",
                    {"collection": "books", "id": rid, "data": {"id": rid}})
    assert [r["status"] for r in results] == ["invalid", "invalid", "invalid", "ok"]
    bad = client.post(f"{URL}/batch", headers=h, json={"changes": "nope"})
    assert bad.status_code == 400
    assert client.get(f"{URL}?since=yesterday", headers=h).status_code == 400


def test_only_admins_change_school_settings(client, app):
    with app.app_context():
        teacher = bearer_for(TEACHER)
    [res] = _push(client, teacher, {"collection": "settings", "id": "_", "data": {"schoolName": "X"}})
    assert res["status"] == "forbidden"
    rid = _rid("att")
    [res] = _push(client, teacher, {"collection": "attendance", "id": rid,
                                    "data": {"id": rid, "studentId": "s", "status": "present"}})
    assert res["status"] == "ok"


def test_parents_and_students_only_see_their_own_records(client, app):
    with app.app_context():
        parent_user = User.query.filter_by(email=PARENT).first()
        parent = Parent.query.filter_by(user_id=parent_user.id).first()
        ward = str(StudentParent.query.filter_by(parent_id=parent.id).first().student_id)
        parent_h, student_h = bearer_for(PARENT), bearer_for(STUDENT)
        teacher_h = bearer_for(TEACHER)
    h = admin_headers()
    own, other, staff_only = _rid("att"), _rid("att"), _rid("lead")
    _push(client, h,
          {"collection": "attendance", "id": own, "data": {"id": own, "studentId": ward}},
          {"collection": "attendance", "id": other, "data": {"id": other, "studentId": str(uuid.uuid4())}},
          {"collection": "marketingLeads", "id": staff_only, "data": {"id": staff_only}})

    parent_records = _pull(client, parent_h)["records"]
    assert _find(parent_records, "attendance", own) is not None
    assert _find(parent_records, "attendance", other) is None
    assert _find(parent_records, "marketingLeads", staff_only) is None
    assert _find(_pull(client, teacher_h)["records"], "attendance", other) is not None

    # Non-staff can't write staff collections or someone else's message.
    msg = _rid("msg")
    results = _push(client, student_h,
                    {"collection": "attendance", "id": _rid("att"), "data": {"studentId": ward}},
                    {"collection": "messages", "id": msg, "data": {"id": msg, "senderId": str(uuid.uuid4())}})
    assert [r["status"] for r in results] == ["forbidden", "forbidden"]
    with app.app_context():
        me = str(User.query.filter_by(email=PARENT).first().id)
    [res] = _push(client, parent_h, {"collection": "messages", "id": msg,
                                     "data": {"id": msg, "senderId": me, "recipientId": "teacher"}})
    assert res["status"] == "ok"


def test_records_are_isolated_per_school(client, app):
    with app.app_context():
        inst = Institution.query.filter_by(code="STXAV-DELHI").first()
        other = Institution(code=f"SYNC-{uuid.uuid4().hex[:6].upper()}", name="Other School",
                            subdomain=f"sync{uuid.uuid4().hex[:6]}", email="office@other.example", status="ACTIVE")
        db.session.add(other)
        db.session.flush()
        rid = _rid("notice")
        db.session.add(SyncRecord(institution_id=other.id, collection="notices", record_id=rid,
                                  data={"id": rid, "title": "secret"}, version=1))
        db.session.commit()
        assert inst.id != other.id
    assert _find(_pull(client, admin_headers())["records"], "notices", rid) is None
    # The same id in this school is a brand-new record, not the other school's.
    [res] = _push(client, admin_headers(), {"collection": "notices", "id": rid, "data": {"id": rid}})
    assert res["status"] == "ok" and res["version"] == 1
