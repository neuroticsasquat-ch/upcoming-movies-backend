"""drop film_availability_current (NEU-1542)

ADR-0023 retires the film page's where-to-watch box (D-29): the site follows a film to its first
home availability and no further, and a box of current carriers promised an accuracy across
time it did not provide. `catalog.film_availability_current` had exactly one reader, the box,
and one writer, the provider poll's snapshot rebuild; both are gone, so the table goes too.

The ledger (`availability_first_seen`), `watch_provider` and `ck_ingest_run_kind`'s `providers`
are untouched — `now_available` still cards off the ledger.

The downgrade recreates the table with its original definition (`7a45131257b9`) but not its
rows, and nothing refills it: the poll no longer writes a snapshot.

Revision ID: db8b0b085ebf
Revises: feb127488dae
Create Date: 2026-10-08 21:15:35.991431+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'db8b0b085ebf'
down_revision = 'feb127488dae'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index('ix_catalog_film_availability_current_film', table_name='film_availability_current', schema='catalog')
    op.drop_table('film_availability_current', schema='catalog')


def downgrade() -> None:
    op.create_table('film_availability_current',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('film_id', sa.UUID(), nullable=False),
    sa.Column('region', sa.Text(), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('monetization_type', sa.Text(), nullable=False),
    sa.Column('link', sa.Text(), nullable=True),
    sa.CheckConstraint("monetization_type IN ('flatrate', 'rent', 'buy')", name='ck_film_availability_current_monetization_type'),
    sa.ForeignKeyConstraint(['film_id'], ['catalog.film.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['provider_id'], ['catalog.watch_provider.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('film_id', 'region', 'provider_id', 'monetization_type', name='uq_film_availability_current'),
    schema='catalog'
    )
    op.create_index('ix_catalog_film_availability_current_film', 'film_availability_current', ['film_id'], unique=False, schema='catalog')
