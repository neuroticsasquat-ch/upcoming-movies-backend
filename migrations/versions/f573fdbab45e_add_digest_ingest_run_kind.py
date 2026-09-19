"""add digest ingest run kind

Revision ID: f573fdbab45e
Revises: b41d7c0fe92a
Create Date: 2026-09-19 12:00:00.000000+00:00

"""
from alembic import op

revision = 'f573fdbab45e'
down_revision = 'b41d7c0fe92a'
branch_labels = None
depends_on = None

# By hand: autogenerate does not diff a CHECK constraint on a table that already exists
# (AGENTS.md). The M7 digest sender opens runs of its own kind (NEU-1381) — one kind for both
# cadences, the detail line says which — so the constraint has to admit it before the first
# `digest daily` or `digest weekly` slot writes a row.
_NEW = "'tmdb', 'feeds', 'link', 'synthesize', 'sweep', 'providers', 'notify', 'digest'"
_OLD = "'tmdb', 'feeds', 'link', 'synthesize', 'sweep', 'providers', 'notify'"


def upgrade() -> None:
    op.execute("ALTER TABLE ingest.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        f"ALTER TABLE ingest.ingest_run ADD CONSTRAINT ck_ingest_run_kind CHECK (kind IN ({_NEW}))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM ingest.ingest_run WHERE kind = 'digest'")
    op.execute("ALTER TABLE ingest.ingest_run DROP CONSTRAINT ck_ingest_run_kind")
    op.execute(
        f"ALTER TABLE ingest.ingest_run ADD CONSTRAINT ck_ingest_run_kind CHECK (kind IN ({_OLD}))"
    )
