"""'add story link state and link ingest kind'

Revision ID: 37f37eb3edeb
Revises: 26f140c4f334
Create Date: 2026-06-20 13:57:25.592897+00:00

"""
from alembic import op
import sqlalchemy as sa


revision = '37f37eb3edeb'
down_revision = '26f140c4f334'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "story",
        sa.Column("link_status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        schema="news",
    )
    op.add_column("story", sa.Column("link_confidence", sa.Float(), nullable=True), schema="news")
    op.add_column(
        "story", sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True), schema="news"
    )
    op.add_column("story", sa.Column("link_note", sa.Text(), nullable=True), schema="news")
    op.create_index("ix_story_link_status", "story", ["link_status"], schema="news")
    op.create_check_constraint(
        "ck_story_link_status",
        "story",
        "link_status IN ('pending', 'linked', 'rejected')",
        schema="news",
    )
    # Alembic doesn't diff CheckConstraints — swap the ingest kind constraint by hand.
    op.drop_constraint("ck_ingest_run_kind", "ingest_run", type_="check", schema="ingest")
    op.create_check_constraint(
        "ck_ingest_run_kind",
        "ingest_run",
        "kind IN ('tmdb', 'feeds', 'link')",
        schema="ingest",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ingest_run_kind", "ingest_run", type_="check", schema="ingest")
    op.create_check_constraint(
        "ck_ingest_run_kind",
        "ingest_run",
        "kind IN ('tmdb', 'feeds')",
        schema="ingest",
    )
    op.drop_constraint("ck_story_link_status", "story", type_="check", schema="news")
    op.drop_index("ix_story_link_status", table_name="story", schema="news")
    op.drop_column("story", "link_note", schema="news")
    op.drop_column("story", "linked_at", schema="news")
    op.drop_column("story", "link_confidence", schema="news")
    op.drop_column("story", "link_status", schema="news")
