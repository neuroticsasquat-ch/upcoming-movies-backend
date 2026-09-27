"""retire import_candidate.skip_reason (NEU-1505)

D-1505.3/D-1505.4: an import never lists a film it cannot follow. A film outside the alert window
is declined into `import_job.unmatched` as `outside_window` instead of being listed unticked, so
every candidate is followable and the column that said otherwise goes. In order:

1. Every candidate with a `skip_reason` is deleted. Left in place with the column gone, those
   rows would read as ordinary candidates and the confirm would follow them. Nothing is lost by
   it: they were never followable, so no confirm could have kept them.
2. `ck_import_candidate_skip_reason` and `ck_import_candidate_skipped_unselected` are dropped.
3. The column is dropped. `selected` stays: it is still the tick the list opens with.

The downgrade re-adds the column (nullable) and both constraints with their original
definitions. There is nothing to restore into it: every row left is followable, which is what
NULL meant.

Revision ID: b3e1c5a7d905
Revises: a92a8e808b07
Create Date: 2026-09-27 21:00:00.000000+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'b3e1c5a7d905'
down_revision = 'a92a8e808b07'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM app.import_candidate WHERE skip_reason IS NOT NULL")
    op.drop_constraint('ck_import_candidate_skipped_unselected', 'import_candidate', schema='app')
    op.drop_constraint('ck_import_candidate_skip_reason', 'import_candidate', schema='app')
    op.drop_column('import_candidate', 'skip_reason', schema='app')


def downgrade() -> None:
    op.add_column(
        'import_candidate', sa.Column('skip_reason', sa.Text(), nullable=True), schema='app'
    )
    op.create_check_constraint(
        'ck_import_candidate_skip_reason',
        'import_candidate',
        "skip_reason IS NULL OR skip_reason IN ('outside_window')",
        schema='app',
    )
    op.create_check_constraint(
        'ck_import_candidate_skipped_unselected',
        'import_candidate',
        "skip_reason IS NULL OR NOT selected",
        schema='app',
    )
