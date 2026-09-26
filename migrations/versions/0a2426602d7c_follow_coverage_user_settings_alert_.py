"""'follow coverage, user_settings alert_stores, drop watchlist_item'

M8 (NEU-1414, ADR-0018): a follow is the only thing a user keeps, and the watchlist is a query
over the follow graph rather than a table.

**The copy in step 3 is not reversed by `downgrade()`, and cannot be.** Every manual or
imported `watchlist_item` becomes a title follow with its own `source` and `created_at`, and
once it is a follow nothing distinguishes it from one the user made on the film page. So the
downgrade drops the two columns and recreates an **empty** `watchlist_item`: it restores the
schema, not the data. `derived_from_follow` rows are deliberately not copied — their follows
are still there and recompute them on read — and per-item `alert_prefs` are not carried
anywhere: the store preference is one per-user setting now (D-44) and starts at `{stream}`.

`app.watchlist_dismissal` is untouched. It is the mute (D-45), and D-40 keeps it.

Revision ID: 0a2426602d7c
Revises: f798756f89c7
Create Date: 2026-09-20 18:11:04.423334+00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0a2426602d7c'
down_revision = 'f798756f89c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. The person coverage tier (D-43). Hand-written CHECK: autogenerate does not emit one
    # for a column added to an existing table, and the parity test compares constraint names.
    op.add_column(
        'follow',
        sa.Column('coverage', sa.Text(), server_default=sa.text("'lead'"), nullable=False),
        schema='app',
    )
    op.create_check_constraint(
        'ck_follow_coverage', 'follow', "coverage IN ('lead', 'all')", schema='app'
    )

    # 2. The one store setting per user (D-44), replacing `watchlist_item.alert_prefs`.
    op.add_column(
        'user_settings',
        sa.Column(
            'alert_stores',
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{stream}'::text[]"),
            nullable=False,
        ),
        schema='app',
    )
    op.create_check_constraint(
        'ck_user_settings_alert_stores',
        'user_settings',
        "alert_stores <@ ARRAY['buy', 'rent', 'stream']::text[]",
        schema='app',
    )

    # 3. Every watchlist row a user or their import chose becomes a title follow, keeping its
    # `source` and its `created_at` — the date the user first showed interest, which is what
    # the computed list sorts on. `ON CONFLICT DO NOTHING`: an existing follow wins, because
    # its `created_at` is at least as old and its `source` is at least as truthful.
    op.execute(
        """
        INSERT INTO app.follow (user_id, entity_type, entity_id, source, coverage, created_at)
        SELECT user_id, 'title', film_id::text, source, 'lead', created_at
        FROM app.watchlist_item
        WHERE source <> 'derived_from_follow'
        ON CONFLICT DO NOTHING
        """
    )

    # 4. The table itself, with its index and constraints.
    op.drop_index(op.f('ix_watchlist_item_film_id'), table_name='watchlist_item', schema='app')
    op.drop_table('watchlist_item', schema='app')


def downgrade() -> None:
    op.drop_constraint('ck_user_settings_alert_stores', 'user_settings', schema='app')
    op.drop_column('user_settings', 'alert_stores', schema='app')
    op.drop_constraint('ck_follow_coverage', 'follow', schema='app')
    op.drop_column('follow', 'coverage', schema='app')
    # Empty, and deliberately so — see the module docstring. The title follows step 3 wrote are
    # left where they are: they are indistinguishable from any other title follow by now.
    op.create_table('watchlist_item',
    sa.Column('user_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('film_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('source', sa.TEXT(), autoincrement=False, nullable=False),
    sa.Column('alert_prefs', postgresql.ARRAY(sa.TEXT()), server_default=sa.text("'{stream}'::text[]"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint("alert_prefs <@ ARRAY['buy'::text, 'rent'::text, 'stream'::text]", name=op.f('ck_watchlist_item_alert_prefs')),
    sa.CheckConstraint("source = ANY (ARRAY['manual'::text, 'derived_from_follow'::text, 'letterboxd_import'::text, 'tmdb_import'::text])", name=op.f('ck_watchlist_item_source')),
    sa.ForeignKeyConstraint(['film_id'], ['catalog.film.id'], name=op.f('watchlist_item_film_id_fkey'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['app.user.id'], name=op.f('watchlist_item_user_id_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'film_id', name=op.f('watchlist_item_pkey')),
    schema='app'
    )
    op.create_index(op.f('ix_watchlist_item_film_id'), 'watchlist_item', ['film_id'], unique=False, schema='app')
