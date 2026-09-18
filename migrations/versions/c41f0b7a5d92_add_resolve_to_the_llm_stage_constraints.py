"""'add resolve to the llm stage check constraints'

Revision ID: c41f0b7a5d92
Revises: 8013f01a60ab
Create Date: 2026-09-18 00:00:00.000000+00:00

"""
from alembic import op

revision = 'c41f0b7a5d92'
down_revision = '8013f01a60ab'
branch_labels = None
depends_on = None

_TABLES = (
    ('run_llm_usage', 'ck_run_llm_usage_stage'),
    ('llm_call', 'ck_llm_call_stage'),
)
_WITH_RESOLVE = "stage IN ('link', 'cluster', 'summarize', 'source_judge', 'resolve')"
_WITHOUT_RESOLVE = "stage IN ('link', 'cluster', 'summarize', 'source_judge')"


def upgrade() -> None:
    for table, constraint in _TABLES:
        op.drop_constraint(constraint, table, schema='ingest')
        op.create_check_constraint(constraint, table, _WITH_RESOLVE, schema='ingest')


def downgrade() -> None:
    # Rows the `resolve` stage wrote would violate the narrowed constraint, so they go with it.
    # A telemetry row for a stage the schema no longer admits is not worth keeping a downgrade
    # from running over.
    op.execute("DELETE FROM ingest.llm_call WHERE stage = 'resolve'")
    op.execute("DELETE FROM ingest.run_llm_usage WHERE stage = 'resolve'")
    for table, constraint in _TABLES:
        op.drop_constraint(constraint, table, schema='ingest')
        op.create_check_constraint(constraint, table, _WITHOUT_RESOLVE, schema='ingest')
