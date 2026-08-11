"""add crew_attached event type

Revision ID: b7c31d9a4e02
Revises: 957421e2651e
Create Date: 2026-08-11 02:10:00.000000+00:00

"""
from alembic import op

revision = 'b7c31d9a4e02'
down_revision = '957421e2651e'
branch_labels = None
depends_on = None

_NEW = (
    "'announced', 'casting', 'crew_attached', 'production_start', 'production_wrap', "
    "'release_date', 'trailer', 'first_look', 'other'"
)
_OLD = (
    "'announced', 'casting', 'production_start', 'production_wrap', "
    "'release_date', 'trailer', 'first_look', 'other'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({_NEW}))")


def downgrade() -> None:
    # A `crew_attached` event cannot satisfy the old constraint, so the rows the credit phase
    # carded go with it — the type stops existing, and there is nothing else they could be.
    op.execute("DELETE FROM news.event WHERE event_type = 'crew_attached'")
    op.execute("ALTER TABLE news.event DROP CONSTRAINT ck_event_type")
    op.execute(f"ALTER TABLE news.event ADD CONSTRAINT ck_event_type CHECK (event_type IN ({_OLD}))")
