import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import (
    String, Text, Numeric, Boolean, Date, DateTime, Integer, UniqueConstraint, CheckConstraint, ForeignKey,
    Index, func
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import BaseModel, TenantModel


class FeeStructure(TenantModel):
    """Institutional fee policy master broken down by class grade and session."""
    __tablename__ = "fee_structures"

    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("academic_years.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    class_level_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("class_levels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    term_frequency: Mapped[str] = mapped_column(String(30), default="ANNUAL", nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    academic_year: Mapped["AcademicYear"] = relationship("AcademicYear")
    class_level: Mapped["ClassLevel"] = relationship("ClassLevel")
    items: Mapped[List["FeeStructureItem"]] = relationship(
        "FeeStructureItem", back_populates="fee_structure", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("institution_id", "academic_year_id", "class_level_id", "name", name="uq_fee_structure_session_class"),
    )


class FeeStructureItem(BaseModel):
    """Line item within a fee structure (e.g. Tuition, Lab, Sports)."""
    __tablename__ = "fee_structure_items"

    fee_structure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fee_structures.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fee_head: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    fee_structure: Mapped["FeeStructure"] = relationship("FeeStructure", back_populates="items")

    __table_args__ = (
        CheckConstraint("amount >= 0", name="chk_fee_item_amount_positive"),
    )


class FeeInvoice(TenantModel):
    """Student billing demand with balance tracking and check constraints."""
    __tablename__ = "fee_invoices"

    invoice_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    fee_structure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fee_structures.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    adjustment_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    balance_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # total = subtotal - discount_amount + fine_amount;
    # balance = total - paid_amount + refunded_amount (a refund re-opens the balance).
    fine_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0", nullable=False
    )
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0", nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="UNPAID", nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    student: Mapped["Student"] = relationship("Student")
    fee_structure: Mapped["FeeStructure"] = relationship("FeeStructure")
    items: Mapped[List["FeeInvoiceItem"]] = relationship(
        "FeeInvoiceItem", back_populates="invoice", cascade="all, delete-orphan"
    )
    payments: Mapped[List["FeePayment"]] = relationship(
        "FeePayment", back_populates="invoice", cascade="all, delete-orphan"
    )
    adjustments: Mapped[List["FeeInvoiceAdjustment"]] = relationship(
        "FeeInvoiceAdjustment", back_populates="invoice", cascade="all, delete-orphan",
        order_by="FeeInvoiceAdjustment.created_at",
    )
    installments: Mapped[List["FeeInstallment"]] = relationship(
        "FeeInstallment", back_populates="invoice", cascade="all, delete-orphan",
        order_by="(FeeInstallment.due_date, FeeInstallment.installment_number)",
    )

    __table_args__ = (
        UniqueConstraint("institution_id", "invoice_number", name="uq_institution_invoice_number"),
        # Payment cannot exceed the invoice total (refunded money may be paid again).
        CheckConstraint("paid_amount <= total_amount + adjustment_amount + refunded_amount",
                        name="chk_invoice_paid_not_exceed_total"),
        CheckConstraint("refunded_amount >= 0 AND refunded_amount <= paid_amount",
                        name="chk_invoice_refunded_range"),
        CheckConstraint("fine_amount >= 0", name="chk_invoice_fine_non_negative"),
        CheckConstraint("paid_amount >= 0", name="chk_invoice_paid_non_negative"),
        CheckConstraint("total_amount >= 0", name="chk_invoice_total_non_negative"),
        CheckConstraint("discount_amount >= 0", name="chk_invoice_discount_non_negative"),
    )


class FeeInvoiceItem(BaseModel):
    """Line item copy on fee invoice."""
    __tablename__ = "fee_invoice_items"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fee_invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fee_head: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    invoice: Mapped["FeeInvoice"] = relationship("FeeInvoice", back_populates="items")


class FeePayment(BaseModel):
    """Recorded fee remittance receipt."""
    __tablename__ = "fee_payments"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fee_invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    payment_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False)  # CASH, CHEQUE, UPI, CARD, ONLINE
    transaction_reference: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED", nullable=False)
    payment_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    recorded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    receipt_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0", nullable=False
    )
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # Checkout attempt that produced this payment (NULL for offline payments).
    order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_orders.id", ondelete="SET NULL", use_alter=True, name="fk_fee_payments_order_id"),
        nullable=True,
        index=True,
    )

    invoice: Mapped["FeeInvoice"] = relationship("FeeInvoice", back_populates="payments")
    recorder: Mapped[Optional["User"]] = relationship("User")
    refunds: Mapped[List["FeeRefund"]] = relationship(
        "FeeRefund", back_populates="payment", order_by="FeeRefund.created_at"
    )
    receipt: Mapped[Optional["FeeReceipt"]] = relationship("FeeReceipt", back_populates="payment", uselist=False)

    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_payment_positive_amount"),
        CheckConstraint("refunded_amount >= 0 AND refunded_amount <= amount", name="chk_payment_refunded_range"),
    )


class FinancialTransaction(TenantModel):
    """Immutable double-entry audit ledger entry."""
    __tablename__ = "financial_transactions"

    payment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fee_payments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    transaction_type: Mapped[str] = mapped_column(String(50), nullable=False)  # FEE_PAYMENT, REFUND, VENDOR_PAYMENT
    category: Mapped[str] = mapped_column(String(50), default="REVENUE", nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    credit_or_debit: Mapped[str] = mapped_column(String(10), nullable=False)  # CREDIT, DEBIT
    description: Mapped[str] = mapped_column(Text, nullable=False)
    entry_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    payment: Mapped[Optional["FeePayment"]] = relationship("FeePayment")

    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_transaction_positive_amount"),
    )


class PaymentGatewayWebhook(BaseModel):
    """Cryptographically verified payment gateway webhook event log."""
    __tablename__ = "payment_gateway_webhooks"

    gateway_provider: Mapped[str] = mapped_column(String(50), nullable=False)  # STRIPE, RAZORPAY
    event_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )


class FeeInvoiceAdjustment(BaseModel):
    """Discount / scholarship / waiver (reduce the total) or late fine (increases it)."""
    __tablename__ = "fee_invoice_adjustments"

    KINDS = ("DISCOUNT", "SCHOLARSHIP", "WAIVER", "LATE_FINE")
    REDUCING_KINDS = ("DISCOUNT", "SCHOLARSHIP", "WAIVER")

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fee_invoices.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(150), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    invoice: Mapped["FeeInvoice"] = relationship("FeeInvoice", back_populates="adjustments")

    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_adjustment_positive_amount"),
        CheckConstraint("kind IN ('DISCOUNT', 'SCHOLARSHIP', 'WAIVER', 'LATE_FINE')", name="chk_adjustment_kind"),
    )


class FeeInstallment(BaseModel):
    """One installment of an invoice; payments are allocated in due-date order."""
    __tablename__ = "fee_installments"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fee_invoices.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    installment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), server_default="0", nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="PENDING", server_default="PENDING", nullable=False)

    invoice: Mapped["FeeInvoice"] = relationship("FeeInvoice", back_populates="installments")

    __table_args__ = (
        UniqueConstraint("invoice_id", "installment_number", name="uq_installment_number"),
        CheckConstraint("amount >= 0", name="chk_installment_amount_non_negative"),
        CheckConstraint("paid_amount >= 0 AND paid_amount <= amount", name="chk_installment_paid_range"),
    )


class PaymentOrder(TenantModel):
    """One online checkout attempt (Razorpay or mock) and its lifecycle."""
    __tablename__ = "payment_orders"

    STATUSES = ("CREATED", "PENDING", "SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED", "NEEDS_REVIEW")

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fee_invoices.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    initiated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # RAZORPAY, MOCK
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    gateway_order_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    gateway_payment_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="CREATED", server_default="CREATED", nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    pending_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    succeeded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fee_payments.id", ondelete="SET NULL"), nullable=True, index=True,
    )

    invoice: Mapped["FeeInvoice"] = relationship("FeeInvoice")
    student: Mapped["Student"] = relationship("Student")
    payment: Mapped[Optional["FeePayment"]] = relationship("FeePayment", foreign_keys=[payment_id])

    __table_args__ = (
        UniqueConstraint("initiated_by", "idempotency_key", name="uq_payment_order_idempotency"),
        CheckConstraint("amount > 0", name="chk_payment_order_positive_amount"),
        CheckConstraint(
            "status IN ('CREATED', 'PENDING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED', 'NEEDS_REVIEW')",
            name="chk_payment_order_status",
        ),
        Index("ix_payment_orders_tenant_created", "institution_id", "created_at"),
    )


class FeeRefund(BaseModel):
    """Refund (partial or full) of a recorded fee payment."""
    __tablename__ = "fee_refunds"

    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fee_payments.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    gateway_refund_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", server_default="PENDING", nullable=False)
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    payment: Mapped["FeePayment"] = relationship("FeePayment", back_populates="refunds")

    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_refund_positive_amount"),
        CheckConstraint("status IN ('PENDING', 'PROCESSED', 'FAILED')", name="chk_refund_status"),
    )


class FeeReceipt(TenantModel):
    """Receipt for a recorded payment with a frozen snapshot of everything printed on it."""
    __tablename__ = "fee_receipts"

    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fee_payments.id", ondelete="RESTRICT"), nullable=False, unique=True,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fee_invoices.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    receipt_number: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(),
        nullable=False,
    )
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)

    payment: Mapped["FeePayment"] = relationship("FeePayment", back_populates="receipt")

    __table_args__ = (
        UniqueConstraint("institution_id", "receipt_number", name="uq_institution_receipt_number"),
    )
