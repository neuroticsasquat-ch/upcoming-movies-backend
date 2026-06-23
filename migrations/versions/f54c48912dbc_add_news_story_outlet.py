"""'add news.story.outlet'

Revision ID: f54c48912dbc
Revises: fc07fca34f59
Create Date: 2026-06-23 22:32:06.471998+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'f54c48912dbc'
down_revision = 'fc07fca34f59'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("story", sa.Column("outlet", sa.Text(), nullable=True), schema="news")

    # Backfill existing Google News rows from the title suffix. Their stored `raw`
    # predates `<source>` capture, so the suffix is all we have. Reuses the same pure
    # helper the live ingest path is built on. Trade-feed rows are left NULL — their
    # `source` is already the outlet, so the read path falls back to it.
    from upmovies.news.outlet import outlet_from_title

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, title FROM news.story "
            "WHERE source LIKE 'Google News:%' AND outlet IS NULL"
        )
    ).fetchall()
    for row in rows:
        outlet = outlet_from_title(row.title)
        if outlet is not None:
            bind.execute(
                sa.text("UPDATE news.story SET outlet = :outlet WHERE id = :id"),
                {"outlet": outlet, "id": row.id},
            )


def downgrade() -> None:
    op.drop_column("story", "outlet", schema="news")
