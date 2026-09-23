"""news.story_entity, and a `kind` in the resolution cache's key (NEU-1445)

EF-12: studios and franchises resolve on the person path's terms. `news.story_entity` is
`story_person`'s shape for the two kinds that are not people, and `news.resolution_cache`
gains `kind` — in its **primary key**, because "Blumhouse" the studio and "Blumhouse" the
franchise are two questions with two answers and one entry could only hold one of them — plus
the `entity_id` those two kinds resolve to.

Three things autogenerate could not see and that are written out by hand below:

- **The primary key swap.** Alembic does not diff primary keys, so the `kind` column arrives
  outside the key unless it is moved in. Existing rows take the `'person'` server default, so
  the widened key is unique over them by construction and the swap needs no backfill.
- **`ck_resolution_cache_kind` and `ck_resolution_cache_id_matches_kind`.** Autogenerate emits
  a table's CHECK constraints when it creates the table (as it did for `story_entity` here) and
  never when it adds a column to an existing one. The second of the two is what makes the two
  id columns exclusive by `kind` rather than merely documented as such.
- **The `NOT VALID` / `VALIDATE` split is deliberately *not* used** for that check: every row
  the table holds took the `'person'` default a moment earlier, so the full scan the plain
  form costs is over a cache that is small by construction and re-fillable by the next run.

Revision ID: ee38f2c9eb2f
Revises: b4c8e2f17a93
Create Date: 2026-09-23 01:49:33.934905+00:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ee38f2c9eb2f"
down_revision = "b4c8e2f17a93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "story_entity",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("story_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("name_as_written", sa.Text(), nullable=False),
        sa.Column("evidence_span", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("kind IN ('company', 'collection')", name="ck_story_entity_kind"),
        sa.CheckConstraint(
            "path IS NULL OR path IN ('accepted', 'tiebreak', 'unlinked', 'not_in_tmdb')",
            name="ck_story_entity_path",
        ),
        sa.ForeignKeyConstraint(["story_id"], ["news.story.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema="news",
    )
    op.create_index(
        "ix_story_entity_kind_entity_id",
        "story_entity",
        ["kind", "entity_id"],
        unique=False,
        schema="news",
    )
    op.create_index(
        "ix_story_entity_story_id", "story_entity", ["story_id"], unique=False, schema="news"
    )

    op.add_column(
        "resolution_cache",
        sa.Column("kind", sa.Text(), server_default=sa.text("'person'"), nullable=False),
        schema="news",
    )
    op.add_column(
        "resolution_cache", sa.Column("entity_id", sa.Integer(), nullable=True), schema="news"
    )
    op.create_check_constraint(
        "ck_resolution_cache_kind",
        "resolution_cache",
        "kind IN ('person', 'company', 'collection')",
        schema="news",
    )
    op.create_check_constraint(
        "ck_resolution_cache_id_matches_kind",
        "resolution_cache",
        "CASE WHEN kind = 'person' THEN entity_id IS NULL ELSE person_id IS NULL END",
        schema="news",
    )
    op.drop_constraint("resolution_cache_pkey", "resolution_cache", type_="primary", schema="news")
    op.create_primary_key(
        "resolution_cache_pkey",
        "resolution_cache",
        ["source_domain", "name_as_written", "film_id", "kind"],
        schema="news",
    )


def downgrade() -> None:
    op.drop_constraint("resolution_cache_pkey", "resolution_cache", type_="primary", schema="news")
    # Every organisation row has to go before the narrower key can be restored: two kinds of
    # the same name on the same film are distinct rows now and would collide under it.
    op.execute("DELETE FROM news.resolution_cache WHERE kind <> 'person'")
    op.create_primary_key(
        "resolution_cache_pkey",
        "resolution_cache",
        ["source_domain", "name_as_written", "film_id"],
        schema="news",
    )
    op.drop_constraint(
        "ck_resolution_cache_id_matches_kind", "resolution_cache", type_="check", schema="news"
    )
    op.drop_constraint(
        "ck_resolution_cache_kind", "resolution_cache", type_="check", schema="news"
    )
    op.drop_column("resolution_cache", "entity_id", schema="news")
    op.drop_column("resolution_cache", "kind", schema="news")
    op.drop_index("ix_story_entity_story_id", table_name="story_entity", schema="news")
    op.drop_index("ix_story_entity_kind_entity_id", table_name="story_entity", schema="news")
    op.drop_table("story_entity", schema="news")
