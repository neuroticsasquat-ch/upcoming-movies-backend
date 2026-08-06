"""Run-tracking for the ingestion pipelines: the DB helpers (pure I/O — callers own commits)
plus `StageCounts`, the pure rule a pipeline consults to decide a run's terminal status."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.models import IngestRun, RunLLMUsage
from upmovies.llm.client import Usage
from upmovies.llm.pricing import price, rates_for

# Every new usage row is sequential — the Message Batches path is gone (ADR-0005).
_BATCHED = False


class StageKind(StrEnum):
    """What a failed item costs a stage — the axis the total-failure rule turns on (NEU-987).

    The rule has no denominator, so with a backlog of *one* "the stage produced nothing" and
    "one item failed" are the same observation. Whether that ambiguity is worth failing a run
    over depends on whether the item gets another chance:

    - `LOSSY` — a failed item gets no second chance, so one failure is enough. A story `link`
      never links stays `pending` and ages out of the recency window after `LINK_RECENCY_DAYS`,
      so a missed run really can lose it. Worth failing on a single failure, ambiguity and all:
      a false alarm costs one noisy deadman ping, a miss costs data.
    - `SELF_HEALING` — a failed item is re-selected unconditionally on the next run, so nothing
      is lost by waiting and a denominator is required. Here the strict rule is actively
      harmful: one permanently unclusterable film on an otherwise empty backlog would fail the
      *whole daily chain* every day, and because `run_daily` is fail-fast, no summaries would
      publish at all for as long as it sat there.

    The trade-off: on a self-healing stage a genuine outage that happens to catch exactly one
    candidate reports `succeeded`. That is deliberate. It is the same "nothing to do" signal a
    quiet day produces, and the next run retries the item regardless.
    """

    LOSSY = "lossy"
    SELF_HEALING = "self_healing"

    @property
    def failure_floor(self) -> int:
        """How many failures a stage of this kind needs before an empty tally is a total
        stage failure."""
        return 2 if self is StageKind.SELF_HEALING else 1


# The classification is a property of the stage, not of any one tally, so it lives here once
# rather than being restated at every construction site (NEU-988). `source_judge` is absent on
# purpose: it is a `link` sub-stage that keeps no counters and is not guarded (CONTEXT.md). Any
# stage named to `total_failure_error` that is missing here raises rather than defaulting, so
# adding a fourth guarded stage forces the lossy/self-healing call instead of inheriting one.
STAGE_KINDS: Mapping[str, StageKind] = {
    "link": StageKind.LOSSY,
    "cluster": StageKind.SELF_HEALING,
    "summarize": StageKind.SELF_HEALING,
}


@dataclass(frozen=True)
class StageCounts:
    """One stage's tally of items that produced output vs. items that failed.

    Stages isolate per-item failures: a chunk (or film, or event) that fails is recorded and
    skipped so the ones that worked still commit. `total_failure` marks the one case where
    that isolation degenerates into "discard everything and report success" — the stage
    produced *nothing at all* yet failed enough items to rule out a quiet day, i.e. a total
    LLM outage rather than a bad item. Runs finalize `failed` on it, which is what makes
    `run_daily` abort and ping the deadman `/fail` instead of summarizing unlinked stories
    (NEU-743, NEU-986).

    Deliberately narrow: a partial failure is still a success, so no proportional threshold.
    Zero processed with zero failed is an idempotent no-op, not an outage. How many failures
    "enough" is depends on the stage's `StageKind`.
    """

    processed: int = 0
    failed: int = 0

    def total_failure(self, kind: StageKind) -> bool:
        if self.processed > 0:
            return False
        return self.failed >= kind.failure_floor


def total_failure_error(**stages: StageCounts) -> str | None:
    """Describe every stage that ended in a `total_failure`, keyed by stage name, or None
    when none did. A pipeline finalizes `failed` iff this returns a message — that is the
    whole of the finalize decision, so it reads the counters rather than assuming success.

    Each keyword must name a stage classified in `STAGE_KINDS`; an unknown one raises."""
    outages = [
        f"{name} stage produced nothing: 0 processed, {counts.failed} failed"
        for name, counts in stages.items()
        if counts.total_failure(_kind_of(name))
    ]
    return "; ".join(outages) or None


def _kind_of(stage: str) -> StageKind:
    try:
        return STAGE_KINDS[stage]
    except KeyError:
        raise ValueError(
            f"unclassified stage {stage!r}: add it to STAGE_KINDS as LOSSY (a failed item is "
            f"never retried) or SELF_HEALING (it is re-selected next run)"
        ) from None


async def create_run(session: AsyncSession, kind: str) -> UUID:
    """Open a new run in the `running` state and return its id. Caller commits."""
    run = IngestRun(kind=kind, status="running")
    session.add(run)
    await session.flush()
    return run.id


async def record_progress(
    session: AsyncSession,
    run_id: UUID,
    *,
    processed_delta: int = 0,
    failed_delta: int = 0,
) -> None:
    """Increment the processed/failed counters and bump last_progress_at."""
    await session.execute(
        update(IngestRun)
        .where(IngestRun.id == run_id)
        .values(
            items_processed=IngestRun.items_processed + processed_delta,
            items_failed=IngestRun.items_failed + failed_delta,
            last_progress_at=datetime.now(UTC),
        )
    )


async def finalize_run(
    session: AsyncSession,
    run_id: UUID,
    *,
    status: str,
    error: str | None = None,
    detail: str | None = None,
) -> None:
    """Move a run to a terminal status and stamp finished_at."""
    values: dict[str, object] = {"status": status, "finished_at": datetime.now(UTC)}
    if error is not None:
        values["error"] = error
    if detail is not None:
        values["detail"] = detail
    await session.execute(update(IngestRun).where(IngestRun.id == run_id).values(**values))


async def mark_stale_runs_cancelled(session: AsyncSession, *, stale_after_minutes: int) -> int:
    """Cancel any run still `running` that started longer ago than the staleness window.
    Returns the number of runs cancelled. Used by startup cleanup to clear runs orphaned
    by a crash/restart."""
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_after_minutes)
    result = await session.execute(
        update(IngestRun)
        .where(IngestRun.status == "running", IngestRun.started_at < cutoff)
        .values(
            status="cancelled",
            finished_at=datetime.now(UTC),
            error="cancelled by startup cleanup (stale run)",
        )
    )
    return result.rowcount or 0  # type: ignore[attr-defined]  # CursorResult has rowcount


async def record_llm_usage(
    session: AsyncSession,
    run_id: UUID,
    *,
    stage: str,
    model: str,
    usage: Usage,
) -> None:
    """UPSERT the per-stage LLM usage + estimated dollar cost for a run. Prices `usage` via
    the shared pricing module (`rates_for(model)` raises KeyError on an unknown model) and
    writes one row per (run_id, stage), overwriting on the uq_run_llm_usage_run_stage
    conflict so a stage re-run refreshes rather than duplicates. Caller owns the commit.

    `batched` is written as a constant `False`: the Message Batches path was removed
    (ADR-0005) so no new row can be batched, but the column stays because historical rows
    recorded under batch mode must keep pricing correctly."""
    cost = Decimal(str(price(usage, rates_for(model), batch=_BATCHED)))
    stmt = pg_insert(RunLLMUsage).values(
        run_id=run_id,
        stage=stage,
        model=model,
        batched=_BATCHED,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cost_usd=cost,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_run_llm_usage_run_stage",
        set_={
            "model": model,
            "batched": _BATCHED,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cost_usd": cost,
        },
    )
    await session.execute(stmt)
