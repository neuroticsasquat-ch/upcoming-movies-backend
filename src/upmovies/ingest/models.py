from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from upmovies.db import Base


class IngestRun(Base):
    """Tracks one execution of an ingestion pipeline. `kind` distinguishes the TMDB
    catalog pipeline from the news-feeds pipeline; both share this operational table,
    which is why it lives in its own `ingest` schema rather than `catalog`/`news`."""

    __tablename__ = "ingest_run"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('tmdb', 'feeds', 'link', 'synthesize')", name="ck_ingest_run_kind"
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ingest_run_status",
        ),
        {"schema": "ingest"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_processed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_usage: Mapped[list["RunLLMUsage"]] = relationship(
        "RunLLMUsage", cascade="all, delete-orphan", back_populates="run"
    )


class RunLLMUsage(Base):
    """Per-stage LLM token usage + estimated dollar cost for one ingest run. A `link`-kind
    run writes a `link` row and a `cluster` row; a `synthesize`-kind run writes a `summarize`
    row. One row per (run, stage) — `record_llm_usage` UPSERTs on the unique constraint."""

    __tablename__ = "run_llm_usage"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('link', 'cluster', 'summarize', 'source_judge')",
            name="ck_run_llm_usage_stage",
        ),
        UniqueConstraint("run_id", "stage", name="uq_run_llm_usage_run_stage"),
        {"schema": "ingest"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ingest.ingest_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    batched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_creation_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    run: Mapped["IngestRun"] = relationship("IngestRun", back_populates="llm_usage")
