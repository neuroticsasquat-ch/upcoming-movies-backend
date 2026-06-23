from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from upmovies.db import Base


class Story(Base):
    """Raw ingested news item. `film_id` is the entity-linking attach point that
    Entity-Linking & Clustering populates later (nullable until linked)."""

    __tablename__ = "story"
    __table_args__ = (
        Index("ix_story_film_id", "film_id"),
        Index("ix_story_link_status", "link_status"),
        CheckConstraint(
            "link_status IN ('pending', 'linked', 'rejected')",
            name="ck_story_link_status",
        ),
        {"schema": "news"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    outlet: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    film_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("catalog.film.id", ondelete="SET NULL"), nullable=True
    )
    link_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    link_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    link_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Event(Base):
    """A distinct per-film news event (a real beat: casting, trailer, release-date change,
    production milestone, …), grouping the stories that report it. The contract Synthesis
    writes summaries against — no summary column lives here."""

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('announced', 'casting', 'production_start', "
            "'production_wrap', 'release_date', 'trailer', 'other')",
            name="ck_event_type",
        ),
        CheckConstraint("confidence IN ('confirmed', 'rumored')", name="ck_event_confidence"),
        Index("ix_event_film_id", "film_id"),
        {"schema": "news"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("catalog.film.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EventStory(Base):
    """Join row: which stories belong to an event. The unique `story_id` enforces the
    one-event-per-story rule (a story attaches to its single dominant beat)."""

    __tablename__ = "event_story"
    __table_args__ = (
        UniqueConstraint("story_id", name="uq_event_story_story_id"),
        {"schema": "news"},
    )

    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("news.event.id", ondelete="CASCADE"), primary_key=True
    )
    story_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("news.story.id", ondelete="CASCADE"), primary_key=True
    )


class EventSummary(Base):
    """AI-generated summary of an event. One summary per event (event_id is the PK)."""

    __tablename__ = "event_summary"
    __table_args__ = ({"schema": "news"},)

    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("news.event.id", ondelete="CASCADE"),
        primary_key=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
