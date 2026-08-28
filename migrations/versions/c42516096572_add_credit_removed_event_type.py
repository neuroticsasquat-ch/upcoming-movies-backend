"""add credit_removed event type

Revision ID: c42516096572
Revises: 542c88d0c3f1
Create Date: 2026-08-28 19:32:42.733197+00:00

"""
from alembic import op

revision = 'c42516096572'
down_revision = '542c88d0c3f1'
branch_labels = None
depends_on = None

_NEW = (
    "'announced', 'casting', 'credit_removed', 'crew_attached', 'production_start', "
    "'production_wrap', 'release_date', 'trailer', 'first_look', 'other'"
)
_OLD = (
    "'announced', 'casting', 'crew_attached', 'production_start', 'production_wrap', "
    "'release_date', 'trailer', 'first_look', 'other'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({_NEW}))")


def downgrade() -> None:
    op.execute("DELETE FROM news.event WHERE event_type = 'credit_removed'")
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({_OLD}))")
