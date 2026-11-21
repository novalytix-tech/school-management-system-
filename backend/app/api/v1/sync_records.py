"""
Server-side persistence for every frontend store collection.

The web client keeps its data in a local store of ~65 collections. Students
(and server-side fee invoices/payments) have dedicated relational endpoints;
everything else is mirrored here, one JSON document per item, so all devices of
a school share the same data.

  GET  /sync/records[?since=<iso>]  -> records changed since a point in time
                                        (or every record, incl. tombstones)
  POST /sync/records/batch          -> apply upserts/deletes with optimistic
                                        concurrency (per-record `base_version`)

Conflicts: each change carries the version the client last saw. If the stored
version differs, the change is NOT applied and the current server copy is
returned, so the client can reload it instead of silently overwriting another
user's edit.
"""
import json
import uuid
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Parent, Student, StudentParent, SyncRecord
from app.services.realtime_service import emit_entity_change
from app.utils.errors import APIException, DatabaseUnavailableError
from app.utils.tenant import get_current_tenant_id

sync_records_bp = Blueprint("sync_records", __name__, url_prefix="/sync/records")

# Collections that are server-authoritative elsewhere or meaningless to share.
EXCLUDED_COLLECTIONS = {"students", "institutions", "version", "migrationAuditResult"}

# Store keys whose value is a single object rather than a list of items.
SINGLETON_ID = "_"

STAFF_ROLES = {"super_admin", "school_admin", "teacher"}
ADMIN_ROLES = {"super_admin", "school_admin"}

# Only school administrators may change school-wide configuration.
ADMIN_WRITE_COLLECTIONS = {
    "settings", "feeStructures", "feePolicy", "academicSessions", "gradingSchemes",
    "schoolServiceConfigs", "serviceEntitlementAuditLogs", "librarySettings",
    "promotionBatches",
}

# Hidden from students, parents and suppliers entirely.
STAFF_READ_COLLECTIONS = {
    "staffAttendance", "staffGroups", "staffGroupMessages", "serviceEntitlementAuditLogs",
    "admissions", "admissionEnquiries", "admissionFollowUps", "admissionAssessments",
    "admissionOffers", "admissionCapacitySettings", "admissionAuditLogs",
    "marketingCampaigns", "marketingLeads", "marketingContentItems", "importBatches",
    "promotionBatches", "recycleBin", "membershipEvents", "libraryInventoryAudits",
    "libraryStaffAssignments",
}

# Records about one student: non-staff only see their own / their wards'.
STUDENT_SCOPED_COLLECTIONS = {
    "attendance", "examMarks", "feeInvoices", "paymentTransactions", "studentEnrollments",
}

# Records owned by a person (field names as the web client stores them).
OWNER_FIELDS = {
    "messages": ("senderId", "recipientId"),
    "reminders": ("userId",),
    "leaveRequests": ("applicantId", "studentId"),
}

# The only collections non-staff users may write (subject to ownership above).
NON_STAFF_WRITE_COLLECTIONS = {
    "messages", "leaveRequests", "reminders", "notifications", "assignments",
    "libraryReservations", "classGroupMessages", "purchaseOrders",
}

MAX_BATCH = 500
MAX_RECORD_BYTES = 2 * 1024 * 1024


def _now():
    return datetime.now(timezone.utc)


def _caller():
    """(user_id, roles, identities) where identities are ids that denote the caller."""
    claims = get_jwt()
    roles = set(claims.get("roles", []))
    user_id = uuid.UUID(get_jwt_identity())
    identities = {str(user_id)}
    student_ids = set()
    if not roles & STAFF_ROLES:
        if "student" in roles:
            student_ids.update(
                str(sid) for (sid,) in db.session.query(Student.id).filter(Student.user_id == user_id)
            )
        if "parent" in roles:
            parent_ids = [str(pid) for (pid,) in db.session.query(Parent.id).filter(Parent.user_id == user_id)]
            identities.update(parent_ids)
            student_ids.update(
                str(sid) for (sid,) in db.session.query(StudentParent.student_id)
                .join(Parent, StudentParent.parent_id == Parent.id)
                .filter(Parent.user_id == user_id)
            )
        identities.update(student_ids)
    return user_id, roles, identities, student_ids


def _is_staff(roles):
    return bool(roles & STAFF_ROLES)


def _can_read(collection, data, roles, identities, student_ids):
    if _is_staff(roles):
        return True
    if collection in STAFF_READ_COLLECTIONS:
        return False
    if not isinstance(data, dict):
        return True
    if collection in STUDENT_SCOPED_COLLECTIONS:
        return str(data.get("studentId")) in student_ids
    fields = OWNER_FIELDS.get(collection)
    if fields:
        return any(str(data.get(f)) in identities for f in fields if data.get(f) is not None)
    return True


def _can_write(collection, data, roles, identities, student_ids):
    if roles & ADMIN_ROLES:
        return True
    if collection in ADMIN_WRITE_COLLECTIONS:
        return False
    if _is_staff(roles):
        return True
    if collection not in NON_STAFF_WRITE_COLLECTIONS:
        return False
    fields = OWNER_FIELDS.get(collection)
    if fields and isinstance(data, dict):
        return any(str(data.get(f)) in identities for f in fields if data.get(f) is not None)
    return True


def _serialize(rec):
    return {
        "collection": rec.collection,
        "id": rec.record_id,
        "data": None if rec.is_deleted else rec.data,
        "version": rec.version,
        "deleted": rec.is_deleted,
        "updated_at": rec.updated_at.isoformat(),
    }


def _parse_since(raw):
    try:
        since = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise APIException("since must be an ISO-8601 timestamp", status_code=400, error_code="INVALID_SINCE")
    return since if since.tzinfo else since.replace(tzinfo=timezone.utc)


@sync_records_bp.route("", methods=["GET"])
def pull_records():
    """Records for the caller's school, filtered to what the caller may see."""
    if not current_app.config.get("DATABASE_ENABLED"):
        raise DatabaseUnavailableError()
    institution_id = get_current_tenant_id(required=True)
    _, roles, identities, student_ids = _caller()
    server_time = _now()

    query = SyncRecord.query.filter(SyncRecord.institution_id == institution_id)
    # Tombstones are included in full pulls too, so a client can tell "emptied on
    # the server" apart from "never synced" for each collection.
    since_raw = request.args.get("since")
    if since_raw:
        query = query.filter(SyncRecord.updated_at > _parse_since(since_raw))

    records = [
        _serialize(r) for r in query.order_by(SyncRecord.created_at.asc()).all()
        # Deleted tombstones carry no data, so they're safe to hand to anyone.
        if r.is_deleted or _can_read(r.collection, r.data, roles, identities, student_ids)
    ]
    initialized = db.session.query(
        SyncRecord.query.filter(SyncRecord.institution_id == institution_id).exists()
    ).scalar()

    return jsonify({"success": True, "data": {
        "server_time": server_time.isoformat(),
        "initialized": bool(initialized),
        "records": records,
    }}), 200


def _validate_change(raw):
    if not isinstance(raw, dict):
        raise ValueError("each change must be an object")
    collection, record_id = raw.get("collection"), raw.get("id")
    if not isinstance(collection, str) or not collection or len(collection) > 64:
        raise ValueError("collection must be a non-empty string")
    if collection in EXCLUDED_COLLECTIONS:
        raise ValueError(f"'{collection}' is not synced through this endpoint")
    if not isinstance(record_id, (str, int)) or isinstance(record_id, bool) or not str(record_id) or len(str(record_id)) > 128:
        raise ValueError("id must be a non-empty string")
    deleted = bool(raw.get("deleted"))
    data = raw.get("data")
    if not deleted:
        if data is None:
            raise ValueError("data is required unless deleted")
        if len(json.dumps(data, separators=(",", ":"))) > MAX_RECORD_BYTES:
            raise ValueError("record is too large")
    base_version = raw.get("base_version") or 0
    if not isinstance(base_version, int) or isinstance(base_version, bool) or base_version < 0:
        raise ValueError("base_version must be a non-negative integer")
    return collection, str(record_id), data, deleted, base_version


@sync_records_bp.route("/batch", methods=["POST"])
def push_records():
    """Apply a batch of upserts/deletes; each change succeeds or conflicts on its own."""
    if not current_app.config.get("DATABASE_ENABLED"):
        raise DatabaseUnavailableError()
    institution_id = get_current_tenant_id(required=True)
    user_id, roles, identities, student_ids = _caller()

    body = request.get_json(silent=True)
    changes = body.get("changes") if isinstance(body, dict) else None
    if not isinstance(changes, list):
        raise APIException("changes must be a list", status_code=400, error_code="INVALID_BODY")
    if len(changes) > MAX_BATCH:
        raise APIException(f"At most {MAX_BATCH} changes per batch", status_code=400, error_code="BATCH_TOO_LARGE")

    results, touched = [], set()
    for raw in changes:
        try:
            collection, record_id, data, deleted, base_version = _validate_change(raw)
        except ValueError as e:
            results.append({"collection": raw.get("collection") if isinstance(raw, dict) else None,
                            "id": raw.get("id") if isinstance(raw, dict) else None,
                            "status": "invalid", "message": str(e)})
            continue

        result = {"collection": collection, "id": record_id}
        savepoint = db.session.begin_nested()
        try:
            rec = (
                SyncRecord.query.filter_by(
                    institution_id=institution_id, collection=collection, record_id=record_id,
                ).with_for_update().first()
            )
            # Ownership is checked against both the new and the stored copy so a
            # user can't take over (or delete) someone else's record.
            if not _can_write(collection, data, roles, identities, student_ids) or (
                rec is not None and not rec.is_deleted
                and not _can_write(collection, rec.data, roles, identities, student_ids)
            ):
                savepoint.rollback()
                result.update(status="forbidden", current=_serialize(rec) if rec is not None else None)
                results.append(result)
                continue

            current_version = rec.version if rec is not None else 0
            if base_version != current_version:
                savepoint.rollback()
                readable = rec is not None and (
                    rec.is_deleted or _can_read(collection, rec.data, roles, identities, student_ids)
                )
                result.update(status="conflict", current=_serialize(rec) if readable else None)
                results.append(result)
                continue

            if rec is None:
                if deleted:
                    # Deleting something the server never had: nothing to do.
                    savepoint.commit()
                    result.update(status="ok", version=0, deleted=True)
                    results.append(result)
                    continue
                rec = SyncRecord(
                    institution_id=institution_id, collection=collection, record_id=record_id,
                    data=data, version=1, is_deleted=False, updated_by=user_id,
                    created_at=_now(), updated_at=_now(),
                )
                db.session.add(rec)
            else:
                rec.version = current_version + 1
                rec.is_deleted = deleted
                rec.data = None if deleted else data
                rec.updated_by = user_id
                rec.updated_at = _now()
            db.session.flush()
            savepoint.commit()
            result.update(status="ok", version=rec.version, deleted=deleted)
            touched.add(collection)
        except IntegrityError:
            # A concurrent insert of the same record won: report it as a conflict.
            savepoint.rollback()
            existing = SyncRecord.query.filter_by(
                institution_id=institution_id, collection=collection, record_id=record_id,
            ).first()
            result.update(status="conflict", current=_serialize(existing) if existing else None)
        results.append(result)

    db.session.commit()
    if touched:
        # `client` identifies the sending device (not the user: one person may be
        # signed in on several devices) so only that device skips the event.
        client_id = body.get("client_id")
        emit_entity_change(institution_id, "sync", "updated", {
            "collections": sorted(touched), "by": str(user_id),
            "client": client_id[:64] if isinstance(client_id, str) else None,
        })
    return jsonify({"success": True, "data": {"server_time": _now().isoformat(), "results": results}}), 200
