"""'add synthesize ingest kind'

Revision ID: fc07fca34f59
Revises: 2655e0fe958c
Create Date: 2026-06-23 17:20:21.021659+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'fc07fca34f59'
down_revision = '2655e0fe958c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_ingest_run_kind", "ingest_run", type_="check", schema="ingest")
    op.create_check_constraint(
        "ck_ingest_run_kind",
        "ingest_run",
        "kind IN ('tmdb', 'feeds', 'link', 'synthesize')",
        schema="ingest",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ingest_run_kind", "ingest_run", type_="check", schema="ingest")
    op.create_check_constraint(
        "ck_ingest_run_kind",
        "ingest_run",
        "kind IN ('tmdb', 'feeds', 'link')",
        schema="ingest",
    )
