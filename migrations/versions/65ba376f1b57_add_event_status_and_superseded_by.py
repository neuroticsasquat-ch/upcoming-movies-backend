"""'add event status and superseded_by'

Revision ID: 65ba376f1b57
Revises: 6112814b95b5
Create Date: 2026-09-17 00:37:18.812838+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '65ba376f1b57'
down_revision = '6112814b95b5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'event',
        sa.Column('status', sa.Text(), server_default=sa.text("'published'"), nullable=False),
        schema='news',
    )
    op.add_column('event', sa.Column('superseded_by', sa.UUID(), nullable=True), schema='news')
    op.create_check_constraint(
        'ck_event_status', 'event', "status IN ('published', 'superseded')", schema='news'
    )
    op.create_index('ix_event_superseded_by', 'event', ['superseded_by'], schema='news')
    op.create_foreign_key(
        'fk_event_superseded_by_event',
        'event',
        'event',
        ['superseded_by'],
        ['id'],
        source_schema='news',
        referent_schema='news',
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_event_superseded_by_event', 'event', schema='news', type_='foreignkey')
    op.drop_index('ix_event_superseded_by', table_name='event', schema='news')
    op.drop_constraint('ck_event_status', 'event', schema='news', type_='check')
    op.drop_column('event', 'superseded_by', schema='news')
    op.drop_column('event', 'status', schema='news')
