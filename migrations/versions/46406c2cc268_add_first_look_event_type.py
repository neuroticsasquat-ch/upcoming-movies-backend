"""add first_look event type

Revision ID: 46406c2cc268
Revises: c8968b592f4c
Create Date: 2026-07-01 00:03:12.780732+00:00

"""
from alembic import op

revision = '46406c2cc268'
down_revision = 'c8968b592f4c'
branch_labels = None
depends_on = None

_NEW = (
    "'announced', 'casting', 'production_start', 'production_wrap', "
    "'release_date', 'trailer', 'first_look', 'other'"
)
_OLD = (
    "'announced', 'casting', 'production_start', 'production_wrap', "
    "'release_date', 'trailer', 'other'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({_NEW}))")


def downgrade() -> None:
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({_OLD}))")
