from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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
            "kind IN ('tmdb', 'feeds', 'link', 'synthesize', 'sweep')",
            name="ck_ingest_run_kind",
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
    llm_calls: Mapped[list["LLMCall"]] = relationship(
        "LLMCall", cascade="all, delete-orphan", back_populates="run"
    )
    retrieval_probes: Mapped[list["LinkRetrievalProbe"]] = relationship(
        "LinkRetrievalProbe", cascade="all, delete-orphan", back_populates="run"
    )
    retrieval_health_row: Mapped["RunRetrievalHealth | None"] = relationship(
        "RunRetrievalHealth", cascade="all, delete-orphan", back_populates="run", uselist=False
    )
    """Named for the row rather than the concept: the run's retrieval *health* as the admin
    surface reads it also carries recall, which is an aggregate over `LinkRetrievalProbe` and
    not on this row. Keeping the names apart stops a read model that mirrors the concept from
    silently extracting this relationship instead (NEU-997)."""


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


class LLMCall(Base):
    """One row per *logical* LLM API call — retries folded in, not split out. Sits alongside
    `RunLLMUsage` rather than replacing it: that table's per-(run, stage) grain structurally
    cannot answer the questions provider selection turns on, because latency is not summable
    and cache-hit is not averageable. Deliberately has no unique (run, stage) constraint —
    many rows per stage is the point.

    Cached tokens are stored as the counts the providers report, not as a `cache_hit` boolean;
    "hit" is a predicate over them. Same shape as `RunLLMUsage`, which keeps `pricing` honest."""

    __tablename__ = "llm_call"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('link', 'cluster', 'summarize', 'source_judge')",
            name="ck_llm_call_stage",
        ),
        CheckConstraint("attempts >= 1", name="ck_llm_call_attempts"),
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
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_creation_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    """Total wall-clock for the logical call, retries included."""
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    """Kept separate from `latency_ms` so retry behaviour stays visible rather than hidden
    inside it."""
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """NULL when the caller performs no JSON parse — distinct from a parse that failed."""
    truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Whether the reply stopped at the `max_tokens` ceiling. What makes `parse_ok=False`
    actionable: truncated and unparseable means raise the ceiling or shrink the batch, while
    not-truncated and unparseable means the model cannot hold the output format. NULL when no
    reply arrived to have been cut off (NEU-1014)."""
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    run: Mapped["IngestRun"] = relationship("IngestRun", back_populates="llm_calls")


class LinkRetrievalProbe(Base):
    """One row per story the **roster linked**, recording what candidate retrieval would
    have offered for it while running in shadow.

    **Historical from the cutover on.** NEU-1004 deleted the roster path and the shadow
    observer with it, so nothing writes this table any more. It is kept because the rows are
    the cutover evidence and `/admin/runs` still reads them for the runs that produced them;
    dropping the table is a separate decision from deleting the code that filled it.

    Grain follows the `LLMCall` precedent rather than the per-run aggregate shape of
    `RunLLMUsage`, because the cutover gate commits to hand-adjudicating a sample of
    disagreements — the roster makes false positives, so retrieval declining to surface one
    is a win a bare percentage scores as a loss. Aggregates cannot support that reading.

    Where the roster rejected and retrieval found nothing, both paths agree and there is
    nothing to inspect, so those are not rows. Linked stories are a minority of a batch,
    which keeps this at tens of rows per run.

    `rank` and `score` are deliberately independent. A pick with a score but no rank was
    lost to the cap (raise K); one with neither never cleared the threshold, which K cannot
    reach at all (ADR-0008) — on the measured corpus those are score-zero misses (§3.1), but
    the column cannot say so, since the retriever reports no score below T. Only
    `RunRetrievalHealth` carries the denominator — the stories that produce it are precisely
    the ones this table does not record."""

    __tablename__ = "link_retrieval_probe"
    __table_args__ = (
        # `retrieved` restates "has a rank"; the constraint stops the two ever disagreeing.
        CheckConstraint("retrieved = (rank IS NOT NULL)", name="ck_link_retrieval_probe_retrieved"),
        CheckConstraint("rank IS NULL OR rank >= 1", name="ck_link_retrieval_probe_rank_positive"),
        # Rank is a position in the set the model was shown, so that set bounds it.
        CheckConstraint(
            "rank IS NULL OR rank <= candidate_count", name="ck_link_retrieval_probe_rank_in_set"
        ),
        # A ranked pick was offered, and nothing is offered without clearing the threshold —
        # so the one pairing the retriever cannot produce is a rank with no score.
        CheckConstraint(
            "rank IS NULL OR score IS NOT NULL", name="ck_link_retrieval_probe_ranked_has_score"
        ),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)", name="ck_link_retrieval_probe_score"
        ),
        CheckConstraint("candidate_count >= 0", name="ck_link_retrieval_probe_candidate_count"),
        # The grain, enforced. Its leading column also serves the per-run reads, so no
        # separate `run_id` index is carried for queries nobody runs.
        UniqueConstraint("run_id", "story_id", name="uq_link_retrieval_probe_run_story"),
        {"schema": "ingest"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ingest.ingest_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    story_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("news.story.id", ondelete="CASCADE"), nullable=False
    )
    film_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("catalog.film.id", ondelete="CASCADE"), nullable=False
    )
    """The film the roster picked — the thing retrieval is being measured against.

    Cascades on delete like `story_id`: the column is NOT NULL because the pick *is* the
    measurement, so there is no surviving row to keep. A film vanishing mid-shadow-period
    would invalidate the roster's pick as an oracle anyway, not merely orphan the probe."""
    retrieved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """Whether the roster's pick was among the candidates the model would have been shown."""
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """1-based position of the pick in the candidate set; NULL when it was not offered."""
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    """The pick's retrieval score, present even when the cap discarded it; NULL when it
    never cleared the threshold."""
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    """Size of the candidate set — post-cap, so it is what the model would have seen."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    run: Mapped["IngestRun"] = relationship("IngestRun", back_populates="retrieval_probes")


class RunRetrievalHealth(Base):
    """Per-run retrieval aggregates: the recall denominator plus the health signals.

    Not derivable from `LinkRetrievalProbe`. That table holds only roster-linked stories,
    while every figure here is drawn from *all* stories retrieval ran over — the
    zero-candidate majority (ADR-0009) contributes nothing to the probe by design.

    Unlike `LinkRetrievalProbe`, this row outlived the roster: every `link` run still writes
    one, and the hard-breach guard (NEU-1002, ADR-0010) reads the same numbers."""

    __tablename__ = "run_retrieval_health"
    __table_args__ = (
        CheckConstraint("stories_retrieved >= 0", name="ck_run_retrieval_health_denominator"),
        # Both signals count stories drawn from the denominator; a rate above 1 would mean a
        # miscount, and would silently corrupt the M4 breach guard built on these columns.
        CheckConstraint(
            "zero_candidate_stories BETWEEN 0 AND stories_retrieved",
            name="ck_run_retrieval_health_zero_candidate",
        ),
        CheckConstraint(
            "saturated_stories BETWEEN 0 AND stories_retrieved",
            name="ck_run_retrieval_health_saturated",
        ),
        # No denominator, no average — NULL rather than a 0.0 that reads as "every story
        # got zero candidates".
        CheckConstraint(
            "(stories_retrieved = 0) = (mean_candidates IS NULL)",
            name="ck_run_retrieval_health_mean",
        ),
        # The grain, enforced — one row per run. Also serves the per-run read on /admin/runs.
        UniqueConstraint("run_id", name="uq_run_retrieval_health_run"),
        {"schema": "ingest"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ingest.ingest_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    stories_retrieved: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    """The denominator: every story retrieval ran over, including those it found nothing for."""
    zero_candidate_stories: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    """Stories retrieval offered nothing for — a rejection without a model call (ADR-0009)."""
    saturated_stories: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    """Stories where the cap discarded a film that had cleared the threshold, so the model
    may never have seen the right one however well it scored."""
    mean_candidates: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Mean candidate-set size per story — post-cap, so it is bounded by K and reads as
    prompt size rather than match volume; `saturated_stories` is what says how often the cap
    bit. NULL when there were no stories to average."""
    soft_breach: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """Whether this run's cap-saturation rate cleared the warn threshold — retrieval health's
    soft tier (ADR-0010, NEU-1088 §3.6). The run still succeeds: saturation is drift, and
    `run_daily` is fail-fast, so a hard tier here would publish no summaries on a day when
    nothing was actually broken.

    **Stored rather than derived** from `saturated_stories / stories_retrieved` at read time.
    The threshold is a setting precisely so it can move as the catalog grows, and a derived
    flag would silently re-judge every historical run against today's value — turning the one
    question the row exists to answer ("did this look wrong *at the time*?") into a moving
    target. False on every row predating the tier, which is accurate: nothing warned."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    run: Mapped["IngestRun"] = relationship("IngestRun", back_populates="retrieval_health_row")
