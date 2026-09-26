"""add catalog search fold columns and trgm indexes (NEU-1469)

Revision ID: de5bd1b49fe2
Revises: 32380c924b81
Create Date: 2026-09-26 00:10:59.031644+00:00

"""
from alembic import op
import sqlalchemy as sa

from upmovies.catalog.fold import fold_computed
from upmovies.catalog.models import INSTALL_FILM_FIELD_CHANGE_TRIGGER


revision = 'de5bd1b49fe2'
down_revision = '32380c924b81'
branch_labels = None
depends_on = None


# (table, source column) for each stored search fold (ADR-0020). The generated expression is
# `fold_computed`'s, not a pasted copy: the parity test compares index definitions but not
# generation expressions, so one constant is what keeps create_all and this file agreeing.
_FOLDS: tuple[tuple[str, str], ...] = (
    ('film', 'title'),
    ('film', 'original_title'),
    ('film_alternative_title', 'title'),
    ('person', 'name'),
    ('person', 'original_name'),
    ('production_company', 'name'),
    ('collection', 'name'),
)


def upgrade() -> None:
    # `title_fold` / `original_title_fold` join FILM_FIELD_CHANGE_DENYLIST: the trigger runs
    # BEFORE UPDATE, when stored generated columns read NULL in NEW, so without the entries
    # every film update would log a fold change. Reinstall first so no update lands between
    # the columns appearing and the function learning to skip them. CREATE OR REPLACE, one
    # op.execute per statement (see the asyncpg note in models.py).
    for stmt in INSTALL_FILM_FIELD_CHANGE_TRIGGER:
        op.execute(stmt)

    # Each ADD COLUMN … STORED rewrites its table under ACCESS EXCLUSIVE — seconds for the
    # 129k-row person table — and no CONCURRENTLY, which cannot run in Alembic's transaction.
    for table, source in _FOLDS:
        fold = f'{source}_fold'
        op.add_column(
            table,
            sa.Column(fold, sa.Text(), fold_computed(source), nullable=True),
            schema='catalog',
        )
        op.create_index(
            f'ix_catalog_{table}_{fold}_trgm',
            table,
            [fold],
            unique=False,
            schema='catalog',
            postgresql_using='gin',
            postgresql_ops={fold: 'gin_trgm_ops'},
        )


def downgrade() -> None:
    # The trigger function keeps its fold denylist entries: inert for columns that no longer
    # exist, and rebuilding the previous body here would fork the one place that SQL is written.
    for table, source in reversed(_FOLDS):
        fold = f'{source}_fold'
        op.drop_index(f'ix_catalog_{table}_{fold}_trgm', table_name=table, schema='catalog')
        op.drop_column(table, fold, schema='catalog')
