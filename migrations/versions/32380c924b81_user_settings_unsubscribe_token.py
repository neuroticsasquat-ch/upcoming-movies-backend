"""app.user_settings.unsubscribe_token (NEU-1463)

DC-10: every digest carries `List-Unsubscribe: <{API_BASE_URL}/digest/unsubscribe/{token}>`,
and the token is this column — text, unique, NOT NULL, like `ical_token` beside it.

**Backfilled here for every existing row**, in Python rather than SQL, so each value comes from
the same generator the app uses (`secrets.token_urlsafe(32)`, `tokens.new_unsubscribe_token`)
rather than from an extension this database may not have. The column goes in nullable, is
filled, then turns NOT NULL. Rows are few — one per subscriber who ever opened their settings.

The unique constraint is named as Postgres would name it (`<table>_<column>_key`), which is
what `create_all` gives the model's `unique=True` — the migration test compares the two by name.

The downgrade drops the column and its constraint; the tokens are gone with it, and every
unsubscribe link already mailed stops resolving (404), which is the correct answer for a schema
that no longer has the feature.

Revision ID: 32380c924b81
Revises: 61b8dca53f8b
Create Date: 2026-09-25 00:30:56.905813+00:00

"""

import secrets

import sqlalchemy as sa
from alembic import op

revision = "32380c924b81"
down_revision = "61b8dca53f8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings", sa.Column("unsubscribe_token", sa.Text(), nullable=True), schema="app"
    )
    bind = op.get_bind()
    user_ids = bind.execute(sa.text("SELECT user_id FROM app.user_settings")).scalars().all()
    for user_id in user_ids:
        bind.execute(
            sa.text("UPDATE app.user_settings SET unsubscribe_token = :token WHERE user_id = :id"),
            {"token": secrets.token_urlsafe(32), "id": user_id},
        )
    op.alter_column("user_settings", "unsubscribe_token", nullable=False, schema="app")
    op.create_unique_constraint(
        "user_settings_unsubscribe_token_key",
        "user_settings",
        ["unsubscribe_token"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint(
        "user_settings_unsubscribe_token_key", "user_settings", schema="app", type_="unique"
    )
    op.drop_column("user_settings", "unsubscribe_token", schema="app")
