"""add film_credit_change.carded_by_event_id (NEU-1371)

Revision ID: f2703d3804bd
Revises: 63ea0fd6e7dd
Create Date: 2026-09-18 17:16:12.533248+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'f2703d3804bd'
down_revision = '63ea0fd6e7dd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The Tier-A short-circuit's link (ADR-0017, D-5): which event published which credit
    # attachment. Nullable with no backfill by design — NULL is the ordinary state, and
    # NEU-1371 is forward-only for the same reason NEU-1205 was: a story card and a catalog
    # card that already both exist for one person are a duplicate this cannot un-publish, so
    # pre-existing pairs are grandfathered rather than guessed at.
    #
    # The one `catalog` -> `news` foreign key. It points the way it does because the fact
    # belongs to the change: the row is the durable ledger of the attachment, and the card is
    # the thing that may later be deleted — hence SET NULL, which loses the attribution but
    # never the history.
    op.add_column('film_credit_change', sa.Column('carded_by_event_id', sa.UUID(), nullable=True), schema='catalog')
    op.create_index('ix_catalog_film_credit_change_carded_by', 'film_credit_change', ['carded_by_event_id'], unique=False, schema='catalog')
    op.create_foreign_key('fk_film_credit_change_carded_by', 'film_credit_change', 'event', ['carded_by_event_id'], ['id'], source_schema='catalog', referent_schema='news', ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_film_credit_change_carded_by', 'film_credit_change', schema='catalog', type_='foreignkey')
    op.drop_index('ix_catalog_film_credit_change_carded_by', table_name='film_credit_change', schema='catalog')
    op.drop_column('film_credit_change', 'carded_by_event_id', schema='catalog')
