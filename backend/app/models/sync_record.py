import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint, Index, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.extensions import db


class SyncRecord(db.Model):
    """One record of a frontend store collection, persisted server-side.

    The web client keeps ~65 collections (teachers, classes, attendance, notices,
    library, transport, ...) in a local store. Each item is mirrored here as a
    JSON document keyed by (institution, collection, record_id) so every device
    of a school sees the same data. ``version`` increments on every write and is
    used for optimistic concurrency: a client must send the version it last saw,
    and a stale write is rejected as a conflict instead of overwriting someone
    else's change. Deletes are soft (``is_deleted``) so other devices learn about
    them on their next incremental pull.
    """
    __tablename__ = "sync_records"
    __table_args__ = (
        UniqueConstraint("institution_id", "collection", "record_id", name="uq_sync_record"),
        Index("ix_sync_records_tenant_updated", "institution_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    institution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="CASCADE"), nullable=False,
    )
    collection: Mapped[str] = mapped_column(String(64), nullable=False)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )
