"""'add sweep ingest kind'

Revision ID: 6fa399bfcd04
Revises: 458ec0c4a35a
Create Date: 2026-08-10 22:03:13.855011+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '6fa399bfcd04'
down_revision = '458ec0c4a35a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_ingest_run_kind", "ingest_run", type_="check", schema="ingest")
    op.create_check_constraint(
        "ck_ingest_run_kind",
        "ingest_run",
        "kind IN ('tmdb', 'feeds', 'link', 'synthesize', 'sweep')",
        schema="ingest",
    )


def downgrade() -> None:
    # Fails loudly if any `sweep` run has been recorded — narrowing the constraint cannot
    # admit those rows. Deliberately not paired with a DELETE: an operator rolling back
    # should decide what happens to the run history, not lose it silently.
    op.drop_constraint("ck_ingest_run_kind", "ingest_run", type_="check", schema="ingest")
    op.create_check_constraint(
        "ck_ingest_run_kind",
        "ingest_run",
        "kind IN ('tmdb', 'feeds', 'link', 'synthesize')",
        schema="ingest",
    )
