"""`app.import_job` rows (D-15, D-16). Repo: pure DB I/O, no commits, no business rules."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import ACTIVE_IMPORT_STATUSES, ImportJob


async def get(db: AsyncSession, job_id: UUID) -> ImportJob | None:
    return await db.get(ImportJob, job_id)


async def get_for_user(db: AsyncSession, *, job_id: UUID, user_id: UUID) -> ImportJob | None:
    """The job, only if it is this user's.

    Scoped in the query rather than fetched and then checked, so a caller cannot forget the
    second half — `GET /me/import/{id}` answers 404 for another user's job, which is both the
    right code and the one that does not confirm the id exists."""
    stmt = select(ImportJob).where(ImportJob.id == job_id, ImportJob.user_id == user_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def active_for_user(db: AsyncSession, user_id: UUID) -> ImportJob | None:
    """This user's import that is still queued or running, if they have one."""
    stmt = select(ImportJob).where(
        ImportJob.user_id == user_id, ImportJob.status.in_(ACTIVE_IMPORT_STATUSES)
    )
    return (await db.execute(stmt)).scalars().first()


async def create(db: AsyncSession, *, user_id: UUID, source: str, rows_total: int) -> ImportJob:
    """Open a job in `queued` and return it. Caller commits."""
    job = ImportJob(user_id=user_id, source=source, status="queued", rows_total=rows_total)
    db.add(job)
    await db.flush()
    await db.refresh(job)
    return job


async def mark_running(db: AsyncSession, job_id: UUID) -> None:
    await db.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id)
        .values(status="running", started_at=datetime.now(UTC))
    )


async def record_progress(
    db: AsyncSession,
    job_id: UUID,
    *,
    rows_done: int,
    watchlist_created: int,
    follows_created: int,
    unmatched: list[dict[str, Any]],
) -> None:
    """Write the running totals onto the row — the heartbeat the UI polls.

    Absolute values rather than deltas, because the runner is the only writer and holds the
    authoritative counts in memory: an increment would have to be emitted exactly once per row
    even when a write is skipped or retried, which is how a progress bar ends up ahead of the
    work. `unmatched` is likewise replaced whole."""
    await db.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id)
        .values(
            rows_done=rows_done,
            watchlist_created=watchlist_created,
            follows_created=follows_created,
            unmatched=unmatched,
        )
    )


async def finalize(
    db: AsyncSession, job_id: UUID, *, status: str, error: str | None = None
) -> None:
    """Move the job to a terminal status and stamp `finished_at`. Caller commits.

    Unconditional, like `ingest.runs.finalize_run`: whatever the row says now, the runner
    finishing is the last word on it."""
    values: dict[str, Any] = {"status": status, "finished_at": datetime.now(UTC)}
    if error is not None:
        values["error"] = error
    await db.execute(update(ImportJob).where(ImportJob.id == job_id).values(**values))
