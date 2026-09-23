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


async def lock_for_user(db: AsyncSession, *, job_id: UUID, user_id: UUID) -> ImportJob | None:
    """`get_for_user`, holding the row lock until the caller commits.

    For the confirm (EF-22), which reads the status and then acts on it: without the lock, a
    second confirm or a new import's discard could move the job between the check and the
    write, and a superseded list would still become follows."""
    stmt = (
        select(ImportJob)
        .where(ImportJob.id == job_id, ImportJob.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def active_for_user(db: AsyncSession, user_id: UUID) -> ImportJob | None:
    """This user's import that is still queued, running or awaiting review, if they have one."""
    stmt = select(ImportJob).where(
        ImportJob.user_id == user_id, ImportJob.status.in_(ACTIVE_IMPORT_STATUSES)
    )
    return (await db.execute(stmt)).scalars().first()


async def create(
    db: AsyncSession,
    *,
    user_id: UUID,
    source: str,
    rows_total: int,
    tmdb_username: str | None = None,
) -> ImportJob:
    """Open a job in `queued` and return it. Caller commits."""
    job = ImportJob(
        user_id=user_id,
        source=source,
        status="queued",
        rows_total=rows_total,
        tmdb_username=tmdb_username,
    )
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


async def mark_awaiting_review(db: AsyncSession, job_id: UUID) -> None:
    """The run is done and its list is waiting on the user (EF-22). Caller commits.

    Not `finalize`: nothing has finished from the user's side yet, so `finished_at` is left for
    the confirm to stamp."""
    await db.execute(
        update(ImportJob).where(ImportJob.id == job_id).values(status="awaiting_review")
    )


async def fail_awaiting_review(db: AsyncSession, *, user_id: UUID, error: str) -> list[UUID]:
    """Move this user's `awaiting_review` job, if any, to `failed` with `error`, and return the
    ids moved. Caller commits.

    Conditional on the status in the statement itself rather than on an earlier read, so a
    confirm that holds the row wins cleanly: Postgres re-checks the `WHERE` once the lock is
    released, and a job that has just become `succeeded` is left alone."""
    stmt = (
        update(ImportJob)
        .where(ImportJob.user_id == user_id, ImportJob.status == "awaiting_review")
        .values(status="failed", error=error, finished_at=datetime.now(UTC))
        .returning(ImportJob.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def mark_confirmed(db: AsyncSession, job_id: UUID, *, follows_created: int) -> None:
    """Move a reviewed job to `succeeded` with the confirmed count (EF-22). Caller commits."""
    await db.execute(
        update(ImportJob)
        .where(ImportJob.id == job_id)
        .values(status="succeeded", follows_created=follows_created, finished_at=datetime.now(UTC))
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


async def set_rows_total(db: AsyncSession, job_id: UUID, rows_total: int) -> None:
    """Write the denominator once the runner knows it. Caller commits.

    Separate from `record_progress` because only one source needs it: an upload knows its row
    count while it is still validating the file, but the TMDB account import (D-16) cannot know
    one until the job is already running and has read both lists."""
    await db.execute(update(ImportJob).where(ImportJob.id == job_id).values(rows_total=rows_total))
