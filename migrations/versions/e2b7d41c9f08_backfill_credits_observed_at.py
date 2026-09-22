"""backfill film.credits_observed_at for films already holding credits

Revision ID: e2b7d41c9f08
Revises: d3f5a81c6b47
Create Date: 2026-09-22 12:00:00.000000+00:00

EF-4 (NEU-1436), D-1436.6. Data only — no schema change, so `tests/integration/
test_migrations.py`'s column/constraint/index parity between `alembic upgrade head` and
`create_all` is untouched, and this is hand-written because autogenerate has nothing to emit.

`957421e2651e` added `credits_observed_at` without stamping the films already in the catalog,
which was harmless while a first observation was a silent baseline. It stops being harmless
here: from this ticket on, a first observation writes an `added` row for every credit of a
followed person, so a film that has not been re-read since that migration would card its
followed credits as if they had just attached.

Stamped only where the film actually holds credit rows. A film with NULL and no credits was
admitted with an empty payload and has genuinely never been observed, so its first credits are
still a baseline — which is the distinction the marker exists to preserve.

The accepted cost (D-1436.6): such a film's next read diffs against credits that may be weeks
stale, so a director who really did attach in the meantime cards as if attached now. Cheaper
than the alternative, which is every followed credit in that population carding at once.

`credits_observed_at` is in `FILM_FIELD_CHANGE_DENYLIST`, so this UPDATE writes no
`film_field_change` row — it neither cards as a public event about our own bookkeeping nor
makes the whole catalog look active to `dormant_film_clause`.
"""

from alembic import op

revision = "e2b7d41c9f08"
down_revision = "d3f5a81c6b47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE catalog.film SET credits_observed_at = now()
        WHERE credits_observed_at IS NULL
          AND EXISTS (SELECT 1 FROM catalog.film_credit c WHERE c.film_id = film.id)
        """
    )


def downgrade() -> None:
    """A no-op, deliberately. The rows this stamped are indistinguishable from the ones an
    ordinary ingest stamped, so clearing the column would re-baseline films that really have
    been observed — a worse state than the one the upgrade fixed."""
