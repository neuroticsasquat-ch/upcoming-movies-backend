"""Run-tracking for the ingestion pipelines: the DB helpers (pure I/O — callers own commits)
plus `StageCounts`, the pure rule a pipeline consults to decide a run's terminal status."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.models import IngestRun, RunLLMUsage
from upmovies.llm.client import Usage
from upmovies.llm.pricing import price, rates_for

# Every new usage row is sequential — the Message Batches path is gone (ADR-0005).
_BATCHED = False


@dataclass(frozen=True)
class StageCounts:
    """One stage's tally of items that produced output vs. items that failed.

    Stages isolate per-item failures: a chunk (or film, or event) that fails is recorded and
    skipped so the ones that worked still commit. `total_failure` marks the one case where
    that isolation degenerates into "discard everything and report success" — the stage
    produced *nothing at all* yet had at least one failure, i.e. a total LLM outage rather
    than a bad item. Runs finalize `failed` on it, which is what makes `run_daily` abort and
    ping the deadman `/fail` instead of summarizing unlinked stories (NEU-743, NEU-986).

    Deliberately narrow: a partial failure is still a success, so no proportional threshold.
    Zero processed with zero failed is an idempotent no-op, not an outage.
    """

    processed: int = 0
    failed: int = 0

    @property
    def total_failure(self) -> bool:
        return self.processed == 0 and self.failed > 0


def total_failure_error(**stages: StageCounts) -> str | None:
    """Describe every stage that ended in a `total_failure`, keyed by stage name, or None
    when none did. A pipeline finalizes `failed` iff this returns a message — that is the
    whole of the finalize decision, so it reads the counters rather than assuming success."""
    outages = [
        f"{name} stage produced nothing: 0 processed, {counts.failed} failed"
        for name, counts in stages.items()
        if counts.total_failure
    ]
    return "; ".join(outages) or None


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
