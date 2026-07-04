"""add story url resolution columns

Revision ID: d1b0671be1d5
Revises: e07c50f357d7
Create Date: 2026-06-30 18:25:54.366153+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'd1b0671be1d5'
down_revision = 'e07c50f357d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('story', sa.Column('resolved_url', sa.Text(), nullable=True), schema='news')
    op.add_column('story', sa.Column('resolve_state', sa.Text(), server_default=sa.text("'none'"), nullable=False), schema='news')
    op.add_column('story', sa.Column('resolve_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False), schema='news')
    op.create_check_constraint(
        'ck_story_resolve_state',
        'story',
        "resolve_state IN ('none', 'pending', 'resolved', 'failed')",
        schema='news',
    )


def downgrade() -> None:
    op.drop_constraint('ck_story_resolve_state', 'story', schema='news', type_='check')
    op.drop_column('story', 'resolve_attempts', schema='news')
    op.drop_column('story', 'resolve_state', schema='news')
    op.drop_column('story', 'resolved_url', schema='news')
