"""
Task 2 tests: Student -> Class+Section integrity enforcement, bulk-import
surfacing of unresolved sections, and the Guardian list endpoint.
Opt-in (runs only with TEST_DATABASE_URL set).

Tenant is pinned to the seeded institution via the X-Institution-Id header so the
tests are deterministic even when other tests add extra institutions (the
endpoints' get_current_tenant_id falls back to an arbitrary ACTIVE institution
otherwise).
"""
import uuid
from datetime import date, datetime, timezone

from app.extensions import db
from app.models import (
    Institution, User, Student, StudentParent, Parent,
    Section, ClassLevel, StudentEnrollment, Role, UserRole,
)
from app.utils.security import hash_password


def _tenant():
    # Seeded demo institution — deterministic regardless of what other tests add.
    return Institution.query.filter_by(code="STXAV-DELHI").first()


def _headers():
    return {"X-Institution-Id": str(_tenant().id)}


def _a_section():
    inst = _tenant()
    return (
        Section.query.join(ClassLevel, Section.class_level_id == ClassLevel.id)
        .filter(ClassLevel.institution_id == inst.id)
        .first()
    )


# --------------------------- create enforcement ---------------------------

def test_create_student_requires_section(client):
    adm = f"ENF-NOSEC-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    res = client.post("/api/v1/students", headers=_headers(), json={
        "admission_number": adm,
        "first_name": "No", "last_name": "Section",
        "gender": "Male", "date_of_birth": "2011-01-01",
    })
    assert res.status_code == 400
    body = res.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "SECTION_REQUIRED"
    assert Student.query.filter_by(admission_number=adm).first() is None


def test_create_student_invalid_section_is_rejected(client):
    adm = f"ENF-BADSEC-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    res = client.post("/api/v1/students", headers=_headers(), json={
        "admission_number": adm,
        "first_name": "Bad", "last_name": "Section",
        "gender": "Female", "date_of_birth": "2011-01-01",
        "section_id": str(uuid.uuid4()),  # well-formed but non-existent
    })
    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "SECTION_NOT_FOUND"
    assert Student.query.filter_by(admission_number=adm).first() is None


def test_create_student_with_valid_section_enrolls(client):
    section = _a_section()
    adm = f"ENF-OK-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    res = client.post("/api/v1/students", headers=_headers(), json={
        "admission_number": adm,
        "first_name": "Good", "last_name": "Record",
        "gender": "Male", "date_of_birth": "2011-01-01",
        "email": f"{adm.lower()}@student.stxaviers.edu.in",
        "section_id": str(section.id),
    })
    assert res.status_code == 201
    data = res.get_json()["data"]
    assert data["class_info"] is not None
    assert data["class_info"]["section_id"] == str(section.id)

    student = Student.query.filter_by(admission_number=adm).first()
    assert student is not None
    active = [e for e in student.enrollments if e.status == "ACTIVE"]
    assert len(active) == 1
    assert str(active[0].section_id) == str(section.id)


# --------------------------- bulk-import enforcement ---------------------------

def test_bulk_import_surfaces_unresolved_section(client):
    section = _a_section()
    cls_name = section.class_level.name
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)

    res = client.post("/api/v1/students/bulk-import", headers=_headers(), json={"students": [
        {
            "fullName": "Imported Ok",
            "admissionNo": f"IMP-OK-{stamp}",
            "className": cls_name, "section": section.name,
            "gender": "Male", "dob": "2012-02-02",
        },
        {
            "fullName": "Imported Bad",
            "admissionNo": f"IMP-BAD-{stamp}",
            "className": "NoSuchClass", "section": "Z",
            "gender": "Female", "dob": "2012-03-03",
        },
    ]})
    assert res.status_code == 201
    data = res.get_json()["data"]
    assert data["imported"] == 1
    assert data["skipped"] == 1
    reasons = " ".join(e["reason"] for e in data["errors"])
    assert "not found" in reasons.lower()

    good = Student.query.filter_by(admission_number=f"IMP-OK-{stamp}").first()
    assert good is not None
    assert any(e.status == "ACTIVE" for e in good.enrollments)
    assert Student.query.filter_by(admission_number=f"IMP-BAD-{stamp}").first() is None


def test_bulk_import_accepts_explicit_section_id(client):
    section = _a_section()
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    adm = f"IMP-SID-{stamp}"
    res = client.post("/api/v1/students/bulk-import", headers=_headers(), json={"students": [{
        "fullName": "SectionId Import", "admissionNo": adm,
        "section_id": str(section.id),  # explicit; no className/section provided
        "gender": "Male", "dob": "2012-01-01",
    }]})
    assert res.status_code == 201
    data = res.get_json()["data"]
    assert data["imported"] == 1
    assert "warnings" in data
    student = Student.query.filter_by(admission_number=adm).first()
    assert student is not None
    assert any(e.status == "ACTIVE" and str(e.section_id) == str(section.id) for e in student.enrollments)


# --------------------------- guardian list endpoint ---------------------------

def test_guardians_list_returns_linked_students(client):
    inst = _tenant()
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)

    suser = User(
        institution_id=inst.id, username=f"gstud_{stamp}",
        email=f"gstud_{stamp}@student.stxaviers.edu.in",
        password_hash=hash_password("Temp#2026!"),
        first_name="Linked", last_name="Child", is_active=True, is_verified=True,
    )
    db.session.add(suser); db.session.flush()
    student = Student(
        institution_id=inst.id, user_id=suser.id,
        admission_number=f"GRD-STU-{stamp}", admission_date=date.today(),
        date_of_birth=date(2012, 1, 1), gender="MALE", status="ACTIVE",
    )
    db.session.add(student); db.session.flush()

    parent = Parent(
        institution_id=inst.id, first_name="Meena", last_name=f"Guardian{stamp}",
        relationship_type="Mother", phone="+91 90000 12345",
        email=f"meena_{stamp}@example.com",
    )
    db.session.add(parent); db.session.flush()
    db.session.add(StudentParent(student_id=student.id, parent_id=parent.id, is_primary_contact=True))
    db.session.commit()

    res = client.get(f"/api/v1/guardians?search=Guardian{stamp}", headers=_headers())
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["total"] >= 1
    match = next((g for g in data["items"] if g["id"] == str(parent.id)), None)
    assert match is not None
    assert match["relationship_type"] == "Mother"
    assert match["student_count"] == 1
    assert match["students"][0]["id"] == str(student.id)
    assert match["students"][0]["admission_number"] == f"GRD-STU-{stamp}"


def test_guardians_list_pagination_shape(client):
    res = client.get("/api/v1/guardians?limit=5&page=1", headers=_headers())
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert set(["items", "total", "page", "limit", "pages"]).issubset(data.keys())
    assert data["limit"] == 5
    assert isinstance(data["items"], list)
