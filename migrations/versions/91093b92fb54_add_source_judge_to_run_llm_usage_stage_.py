"""'add source_judge to run_llm_usage stage constraint'

Revision ID: 91093b92fb54
Revises: b0b9de2b93a2
Create Date: 2026-07-01 19:54:20.457657+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '91093b92fb54'
down_revision = 'b0b9de2b93a2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('ck_run_llm_usage_stage', 'run_llm_usage', schema='ingest')
    op.create_check_constraint(
        'ck_run_llm_usage_stage',
        'run_llm_usage',
        "stage IN ('link', 'cluster', 'summarize', 'source_judge')",
        schema='ingest',
    )


def downgrade() -> None:
    op.drop_constraint('ck_run_llm_usage_stage', 'run_llm_usage', schema='ingest')
    op.create_check_constraint(
        'ck_run_llm_usage_stage',
        'run_llm_usage',
        "stage IN ('link', 'cluster', 'summarize')",
        schema='ingest',
    )
