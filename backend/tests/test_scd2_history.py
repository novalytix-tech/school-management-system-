"""
Task 3 tests: SCD Type 2 history for students.
Editing creates a new current version while the prior one stays queryable with
correct validity dates; deletes record a final DELETE version; imports seed an
initial version. Opt-in (runs only with TEST_DATABASE_URL set).
"""
from datetime import datetime, timezone

from app.models import Student, StudentHistory, Section, ClassLevel, Institution


def _a_section():
    # A section in the first ACTIVE institution (the tenant the endpoint resolves),
    # so the test is robust when other tests add extra institutions.
    inst = Institution.query.filter_by(status="ACTIVE").first()
    return (
        Section.query.join(ClassLevel, Section.class_level_id == ClassLevel.id)
        .filter(ClassLevel.institution_id == inst.id)
        .first()
    )


def _section_id():
    return str(_a_section().id)


def _create(client, suffix, first="Orig", last="Name"):
    adm = f"SCD-{suffix}-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    res = client.post("/api/v1/students", json={
        "admission_number": adm,
        "first_name": first, "last_name": last,
        "gender": "Male", "date_of_birth": "2011-01-01",
        "email": f"{adm.lower()}@student.stxaviers.edu.in",
        "section_id": _section_id(),
    })
    assert res.status_code == 201, res.get_json()
    return res.get_json()["data"]["id"], adm


def test_create_records_initial_version(client):
    sid, adm = _create(client, "CREATE")
    res = client.get(f"/api/v1/students/{sid}/history")
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["total"] == 1
    v = data["versions"][0]
    assert v["version_number"] == 1
    assert v["operation"] == "CREATE"
    assert v["is_current"] is True
    assert v["valid_to"] is None
    assert v["class_name"] and v["section_name"]  # enrollment snapshot captured


def test_edit_creates_new_current_version_prior_stays_queryable(client):
    sid, adm = _create(client, "EDIT", first="Before")

    upd = client.put(f"/api/v1/students/{sid}", json={"first_name": "After"})
    assert upd.status_code == 200

    data = client.get(f"/api/v1/students/{sid}/history").get_json()["data"]
    assert data["total"] == 2
    newest, prior = data["versions"][0], data["versions"][1]

    # New current version reflects the edit.
    assert newest["version_number"] == 2
    assert newest["operation"] == "UPDATE"
    assert newest["is_current"] is True
    assert newest["valid_to"] is None
    assert newest["first_name"] == "After"

    # Prior version is closed but still queryable with its original value + dates.
    assert prior["version_number"] == 1
    assert prior["is_current"] is False
    assert prior["valid_to"] is not None
    assert prior["first_name"] == "Before"

    # Exactly one current version in the DB.
    current = StudentHistory.query.filter_by(student_id=sid, is_current=True).count()
    assert current == 1


def test_multiple_edits_keep_single_current(client):
    sid, _ = _create(client, "MULTI")
    for name in ["A", "B", "C"]:
        assert client.put(f"/api/v1/students/{sid}", json={"first_name": name}).status_code == 200

    data = client.get(f"/api/v1/students/{sid}/history").get_json()["data"]
    assert data["total"] == 4  # 1 create + 3 edits
    assert sum(1 for v in data["versions"] if v["is_current"]) == 1
    assert data["versions"][0]["first_name"] == "C"


def test_delete_records_delete_version(client):
    sid, _ = _create(client, "DEL")
    assert client.delete(f"/api/v1/students/{sid}").status_code == 200

    data = client.get(f"/api/v1/students/{sid}/history").get_json()["data"]
    newest = data["versions"][0]
    assert newest["operation"] == "DELETE"
    assert newest["is_current"] is True
    assert newest["is_deleted"] is True
    assert newest["status"] == "WITHDRAWN"


def test_bulk_import_seeds_initial_version(client):
    section = _a_section()
    cls_name = section.class_level.name
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    adm = f"SCD-IMP-{stamp}"
    res = client.post("/api/v1/students/bulk-import", json={"students": [{
        "fullName": "Hist Imported", "admissionNo": adm,
        "className": cls_name, "section": section.name,
        "gender": "Female", "dob": "2012-05-05",
    }]})
    assert res.status_code == 201
    assert res.get_json()["data"]["imported"] == 1

    student = Student.query.filter_by(admission_number=adm).first()
    hist = StudentHistory.query.filter_by(student_id=student.id).all()
    assert len(hist) == 1
    assert hist[0].operation == "CREATE"
    assert hist[0].is_current is True
    assert hist[0].version_number == 1


def test_history_404_for_unknown_student(client):
    import uuid
    res = client.get(f"/api/v1/students/{uuid.uuid4()}/history")
    assert res.status_code == 404
