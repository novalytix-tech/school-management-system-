import uuid
from datetime import date, datetime, timezone
from typing import Optional, List
from sqlalchemy import String, Text, Boolean, Date, DateTime, Integer, UniqueConstraint, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.extensions import db
from app.models.base import BaseModel, TenantModel


class Student(TenantModel):
    """Enrolled student master profile."""
    __tablename__ = "students"

    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    admission_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    admission_date: Mapped[date] = mapped_column(Date, nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    gender: Mapped[str] = mapped_column(String(20), nullable=False)  # MALE, FEMALE, OTHER
    blood_group: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    nationality: Mapped[str] = mapped_column(String(50), default="Indian", nullable=False)
    religion: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    caste_category: Mapped[Optional[str]] = mapped_column(String(50), default="GENERAL", nullable=True)
    emergency_contact_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    emergency_contact_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    medical_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)  # ACTIVE, WITHDRAWN, GRADUATED

    # Relationships
    user: Mapped[Optional["User"]] = relationship("User")
    student_parents: Mapped[List["StudentParent"]] = relationship(
        "StudentParent", back_populates="student", cascade="all, delete-orphan"
    )
    enrollments: Mapped[List["StudentEnrollment"]] = relationship(
        "StudentEnrollment", back_populates="student", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("institution_id", "admission_number", name="uq_institution_admission_number"),
    )


class Parent(TenantModel):
    """Parent or guardian profile."""
    __tablename__ = "parents"

    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(50), nullable=False)  # Father, Mother, Guardian
    occupation: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    annual_income: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    user: Mapped[Optional["User"]] = relationship("User")
    student_parents: Mapped[List["StudentParent"]] = relationship(
        "StudentParent", back_populates="parent", cascade="all, delete-orphan"
    )


class StudentParent(BaseModel):
    """Many-to-many relationship linking students to parents/guardians."""
    __tablename__ = "student_parents"

    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_primary_contact: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_pickup: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    student: Mapped["Student"] = relationship("Student", back_populates="student_parents")
    parent: Mapped["Parent"] = relationship("Parent", back_populates="student_parents")

    __table_args__ = (
        UniqueConstraint("student_id", "parent_id", name="uq_student_parent"),
    )


class StudentEnrollment(BaseModel):
    """Historical and active cohort enrollment per academic session."""
    __tablename__ = "student_enrollments"

    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("academic_years.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    roll_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    enrollment_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)  # ACTIVE, PROMOTED, TRANSFERRED

    student: Mapped["Student"] = relationship("Student", back_populates="enrollments")
    academic_year: Mapped["AcademicYear"] = relationship("AcademicYear")
    section: Mapped["Section"] = relationship("Section")

    __table_args__ = (
        # Mandatory constraint: One student cannot have duplicate active enrollment in same academic year
        UniqueConstraint("student_id", "academic_year_id", name="uq_student_academic_year"),
    )


class StudentPromotion(BaseModel):
    """Audit log of student promotion from one academic session/class to the next."""
    __tablename__ = "student_promotions"

    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_academic_year_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("academic_years.id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_academic_year_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("academic_years.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sections.id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sections.id", ondelete="RESTRICT"),
        nullable=False,
    )
    promotion_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    decision: Mapped[str] = mapped_column(String(30), default="PROMOTED", nullable=False)  # PROMOTED, DETAINED
    remarks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    promoted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class StudentHistory(db.Model):
    """Slowly Changing Dimension (Type 2) history for students.

    Every create / edit / soft-delete of a student appends an immutable version
    row here. The current version has ``valid_to IS NULL`` and ``is_current =
    True``; an edit closes the prior version (sets ``valid_to`` and clears
    ``is_current``) and inserts a new current one. The surrogate key ``id`` is
    separate from the business key (``admission_number`` / ``student_id``).

    History is written by the application service layer (app.services
    .student_history_service) on every student write path, and is never deleted.
    """
    __tablename__ = "student_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Business key / link to the live record
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    admission_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Snapshot of the student's attributes at this version
    first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    blood_group: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    student_is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Snapshot of the active enrollment (class/section)
    section_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    class_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    section_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    roll_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # SCD2 version metadata
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)  # CREATE, UPDATE, DELETE
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_student_history_student_current", "student_id", "is_current"),
    )

