"""Run-tracking helpers for the ingestion pipelines. Pure DB I/O — callers own commits."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.models import IngestRun


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
