"""add sync_records table (server-side store for all frontend collections)

Revision ID: 9c2f4e1a7b30
Revises: 774db7963eda
Create Date: 2026-11-21 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '9c2f4e1a7b30'
down_revision = '774db7963eda'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('sync_records',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('institution_id', sa.UUID(), nullable=False),
    sa.Column('collection', sa.String(length=64), nullable=False),
    sa.Column('record_id', sa.String(length=128), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('is_deleted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'collection', 'record_id', name='uq_sync_record')
    )
    with op.batch_alter_table('sync_records', schema=None) as batch_op:
        batch_op.create_index('ix_sync_records_tenant_updated', ['institution_id', 'updated_at'], unique=False)


def downgrade():
    with op.batch_alter_table('sync_records', schema=None) as batch_op:
        batch_op.drop_index('ix_sync_records_tenant_updated')

    op.drop_table('sync_records')
