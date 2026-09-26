"""drop app.watchlist_dismissal

Revision ID: b4c8e2f17a93
Revises: e2b7d41c9f08
Create Date: 2026-09-22 15:00:00.000000+00:00

EF-14 (NEU-1439). The mute goes with the watchlist it subtracted from.

A mute only ever meant "this film is on my list and I do not want it" — a correction to a set
the user did not assemble. Under EF-3 nothing indirect reaches a film any more: the set is
exactly the titles the user followed by name, so the correction to it is **unfollowing**, and a
table that silences a film the user is still following describes a state no surface can now
produce.

**The rows are dropped, not migrated.** There is nothing to migrate them *to*: turning a mute
into an unfollow would delete a follow the user made deliberately, and D-40's rule that nothing
here deletes user graph rows cuts against exactly that. A mute on a film reached only through a
followed director has no successor at all — that film simply leaves the list, mute or no mute.
So the honest end state is the table gone and every follow left standing; a user who had
silenced a film they also follow by title sees it again, which is the one visible consequence
and is the state EF-14 defines.

Hand-written, like every `DROP TABLE` here: autogenerate would emit the drop but not the
reasoning, and the downgrade has to restate the DDL because the model no longer declares it.
"""

import sqlalchemy as sa
from alembic import op

revision = "b4c8e2f17a93"
down_revision = "e2b7d41c9f08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("watchlist_dismissal", schema="app")


def downgrade() -> None:
    """Recreates the table empty. The rows cannot come back — nothing else records that a user
    had silenced a film — so a downgrade restores the shape and not the state, which is the most
    a drop of user data can honestly promise."""
    op.create_table(
        "watchlist_dismissal",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("film_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["film_id"], ["catalog.film.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app.user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "film_id"),
        schema="app",
    )
