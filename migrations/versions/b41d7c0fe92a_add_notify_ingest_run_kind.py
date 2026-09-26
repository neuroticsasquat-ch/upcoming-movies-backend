"""add notify ingest run kind

Revision ID: b41d7c0fe92a
Revises: 6dacd65483eb
Create Date: 2026-09-18 23:20:00.000000+00:00

"""
from alembic import op

revision = 'b41d7c0fe92a'
down_revision = '6dacd65483eb'
branch_labels = None
depends_on = None

# By hand: autogenerate does not diff a CHECK constraint on a table that already exists
# (AGENTS.md). The M7 decision pass opens runs of its own kind, and its watermark is the last
# run carrying that kind — so the constraint has to admit it before the first one is written.
_NEW = "'tmdb', 'feeds', 'link', 'synthesize', 'sweep', 'providers', 'notify'"
_OLD = "'tmdb', 'feeds', 'link', 'synthesize', 'sweep', 'providers'"


def upgrade() -> None:
    op.execute("ALTER TABLE ingest.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        f"ALTER TABLE ingest.ingest_run ADD CONSTRAINT ck_ingest_run_kind CHECK (kind IN ({_NEW}))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM ingest.ingest_run WHERE kind = 'notify'")
    op.execute("ALTER TABLE ingest.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        f"ALTER TABLE ingest.ingest_run ADD CONSTRAINT ck_ingest_run_kind CHECK (kind IN ({_OLD}))"
    )
