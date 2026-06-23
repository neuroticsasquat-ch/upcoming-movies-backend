"""'add catalog.film.slug'

Revision ID: 2655e0fe958c
Revises: 72cefc645fdc
Create Date: 2026-06-23 00:21:25.890826+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '2655e0fe958c'
down_revision = '72cefc645fdc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("film", sa.Column("slug", sa.Text(), nullable=True), schema="catalog")

    # Backfill existing rows in tmdb_id order (deterministic: lowest tmdb_id wins the clean base),
    # reusing the same pure helper the live insert path is built on.
    from upmovies.catalog.slug import backfill_slugs

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, title, release_date, tmdb_id FROM catalog.film ORDER BY tmdb_id")
    ).fetchall()
    for film_id, slug in backfill_slugs([(r.id, r.title, r.release_date, r.tmdb_id) for r in rows]):
        bind.execute(
            sa.text("UPDATE catalog.film SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": film_id},
        )

    op.create_index("ix_catalog_film_slug", "film", ["slug"], unique=True, schema="catalog")


def downgrade() -> None:
    op.drop_index("ix_catalog_film_slug", table_name="film", schema="catalog")
    op.drop_column("film", "slug", schema="catalog")
