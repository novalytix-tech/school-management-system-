"""fee payments: installments, adjustments, payment orders, refunds, receipts

Revision ID: b7d3e5a91c42
Revises: 9c2f4e1a7b30
Create Date: 2026-11-21 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'b7d3e5a91c42'
down_revision = '9c2f4e1a7b30'
branch_labels = None
depends_on = None


def upgrade():
    # --- fee_invoices: fines and refunds -------------------------------------------
    with op.batch_alter_table('fee_invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fine_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('refunded_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))
        batch_op.drop_constraint('chk_invoice_paid_not_exceed_total', type_='check')
        batch_op.create_check_constraint(
            'chk_invoice_paid_not_exceed_total',
            'paid_amount <= total_amount + adjustment_amount + refunded_amount')
        batch_op.create_check_constraint(
            'chk_invoice_refunded_range', 'refunded_amount >= 0 AND refunded_amount <= paid_amount')
        batch_op.create_check_constraint('chk_invoice_fine_non_negative', 'fine_amount >= 0')

    # --- fee_invoice_adjustments ---------------------------------------------------
    op.create_table('fee_invoice_adjustments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('invoice_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('label', sa.String(length=150), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount > 0', name='chk_adjustment_positive_amount'),
    sa.CheckConstraint("kind IN ('DISCOUNT', 'SCHOLARSHIP', 'WAIVER', 'LATE_FINE')", name='chk_adjustment_kind'),
    sa.ForeignKeyConstraint(['invoice_id'], ['fee_invoices.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('fee_invoice_adjustments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fee_invoice_adjustments_invoice_id'), ['invoice_id'], unique=False)

    # --- fee_installments ----------------------------------------------------------
    op.create_table('fee_installments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('invoice_id', sa.UUID(), nullable=False),
    sa.Column('installment_number', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('paid_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
    sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount >= 0', name='chk_installment_amount_non_negative'),
    sa.CheckConstraint('paid_amount >= 0 AND paid_amount <= amount', name='chk_installment_paid_range'),
    sa.ForeignKeyConstraint(['invoice_id'], ['fee_invoices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('invoice_id', 'installment_number', name='uq_installment_number')
    )
    with op.batch_alter_table('fee_installments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fee_installments_invoice_id'), ['invoice_id'], unique=False)

    # --- payment_orders ------------------------------------------------------------
    op.create_table('payment_orders',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('institution_id', sa.UUID(), nullable=False),
    sa.Column('invoice_id', sa.UUID(), nullable=False),
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('initiated_by', sa.UUID(), nullable=True),
    sa.Column('provider', sa.String(length=20), nullable=False),
    sa.Column('is_test', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('gateway_order_id', sa.String(length=64), nullable=True),
    sa.Column('gateway_payment_id', sa.String(length=64), nullable=True),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default='INR', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='CREATED', nullable=False),
    sa.Column('failure_reason', sa.String(length=255), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('pending_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('succeeded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('payment_id', sa.UUID(), nullable=True),
    sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount > 0', name='chk_payment_order_positive_amount'),
    sa.CheckConstraint(
        "status IN ('CREATED', 'PENDING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED', 'NEEDS_REVIEW')",
        name='chk_payment_order_status'),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['invoice_id'], ['fee_invoices.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['initiated_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['payment_id'], ['fee_payments.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('gateway_order_id'),
    sa.UniqueConstraint('gateway_payment_id'),
    sa.UniqueConstraint('initiated_by', 'idempotency_key', name='uq_payment_order_idempotency')
    )
    with op.batch_alter_table('payment_orders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payment_orders_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_orders_invoice_id'), ['invoice_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_orders_student_id'), ['student_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_orders_initiated_by'), ['initiated_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_payment_orders_payment_id'), ['payment_id'], unique=False)
        batch_op.create_index('ix_payment_orders_tenant_created', ['institution_id', 'created_at'], unique=False)

    # --- fee_payments: refunds, test flag, order link --------------------------------
    with op.batch_alter_table('fee_payments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('refunded_amount', sa.Numeric(precision=12, scale=2), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('is_test', sa.Boolean(), server_default='false', nullable=False))
        batch_op.add_column(sa.Column('order_id', sa.UUID(), nullable=True))
        batch_op.create_index(batch_op.f('ix_fee_payments_order_id'), ['order_id'], unique=False)
        batch_op.create_foreign_key('fk_fee_payments_order_id', 'payment_orders', ['order_id'], ['id'], ondelete='SET NULL')
        batch_op.create_check_constraint(
            'chk_payment_refunded_range', 'refunded_amount >= 0 AND refunded_amount <= amount')
    # Payment statuses are now SUCCESS | PARTIALLY_REFUNDED | REFUNDED.
    op.execute("UPDATE fee_payments SET status = 'SUCCESS' WHERE status = 'COMPLETED'")

    # --- fee_refunds ---------------------------------------------------------------
    op.create_table('fee_refunds',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('payment_id', sa.UUID(), nullable=False),
    sa.Column('gateway_refund_id', sa.String(length=64), nullable=True),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount > 0', name='chk_refund_positive_amount'),
    sa.CheckConstraint("status IN ('PENDING', 'PROCESSED', 'FAILED')", name='chk_refund_status'),
    sa.ForeignKeyConstraint(['payment_id'], ['fee_payments.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('gateway_refund_id')
    )
    with op.batch_alter_table('fee_refunds', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fee_refunds_payment_id'), ['payment_id'], unique=False)

    # --- fee_receipts --------------------------------------------------------------
    op.create_table('fee_receipts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('institution_id', sa.UUID(), nullable=False),
    sa.Column('payment_id', sa.UUID(), nullable=False),
    sa.Column('invoice_id', sa.UUID(), nullable=False),
    sa.Column('student_id', sa.UUID(), nullable=False),
    sa.Column('receipt_number', sa.String(length=32), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('is_test', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['payment_id'], ['fee_payments.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['invoice_id'], ['fee_invoices.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('payment_id'),
    sa.UniqueConstraint('institution_id', 'receipt_number', name='uq_institution_receipt_number')
    )
    with op.batch_alter_table('fee_receipts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fee_receipts_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_fee_receipts_invoice_id'), ['invoice_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_fee_receipts_student_id'), ['student_id'], unique=False)


def downgrade():
    with op.batch_alter_table('fee_receipts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fee_receipts_student_id'))
        batch_op.drop_index(batch_op.f('ix_fee_receipts_invoice_id'))
        batch_op.drop_index(batch_op.f('ix_fee_receipts_institution_id'))
    op.drop_table('fee_receipts')

    with op.batch_alter_table('fee_refunds', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fee_refunds_payment_id'))
    op.drop_table('fee_refunds')

    op.execute("UPDATE fee_payments SET status = 'COMPLETED' WHERE status = 'SUCCESS'")
    with op.batch_alter_table('fee_payments', schema=None) as batch_op:
        batch_op.drop_constraint('chk_payment_refunded_range', type_='check')
        batch_op.drop_constraint('fk_fee_payments_order_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_fee_payments_order_id'))
        batch_op.drop_column('order_id')
        batch_op.drop_column('is_test')
        batch_op.drop_column('refunded_amount')

    with op.batch_alter_table('payment_orders', schema=None) as batch_op:
        batch_op.drop_index('ix_payment_orders_tenant_created')
        batch_op.drop_index(batch_op.f('ix_payment_orders_payment_id'))
        batch_op.drop_index(batch_op.f('ix_payment_orders_initiated_by'))
        batch_op.drop_index(batch_op.f('ix_payment_orders_student_id'))
        batch_op.drop_index(batch_op.f('ix_payment_orders_invoice_id'))
        batch_op.drop_index(batch_op.f('ix_payment_orders_institution_id'))
    op.drop_table('payment_orders')

    with op.batch_alter_table('fee_installments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fee_installments_invoice_id'))
    op.drop_table('fee_installments')

    with op.batch_alter_table('fee_invoice_adjustments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fee_invoice_adjustments_invoice_id'))
    op.drop_table('fee_invoice_adjustments')

    with op.batch_alter_table('fee_invoices', schema=None) as batch_op:
        batch_op.drop_constraint('chk_invoice_fine_non_negative', type_='check')
        batch_op.drop_constraint('chk_invoice_refunded_range', type_='check')
        batch_op.drop_constraint('chk_invoice_paid_not_exceed_total', type_='check')
        batch_op.create_check_constraint(
            'chk_invoice_paid_not_exceed_total', 'paid_amount <= total_amount + adjustment_amount')
        batch_op.drop_column('refunded_amount')
        batch_op.drop_column('fine_amount')
