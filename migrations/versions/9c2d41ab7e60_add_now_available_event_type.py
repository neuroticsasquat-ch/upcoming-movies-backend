"""add now_available event type

Revision ID: 9c2d41ab7e60
Revises: 7a45131257b9
Create Date: 2026-09-18 20:45:00.000000+00:00

"""
from alembic import op

revision = '9c2d41ab7e60'
down_revision = '7a45131257b9'
branch_labels = None
depends_on = None

_NEW = (
    "'announced', 'casting', 'credit_removed', 'crew_attached', 'now_available', "
    "'production_start', 'production_wrap', 'release_date', 'trailer', 'first_look', 'other'"
)
_OLD = (
    "'announced', 'casting', 'credit_removed', 'crew_attached', 'production_start', "
    "'production_wrap', 'release_date', 'trailer', 'first_look', 'other'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({_NEW}))")


def downgrade() -> None:
    op.execute("DELETE FROM news.event WHERE event_type = 'now_available'")
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({_OLD}))")
