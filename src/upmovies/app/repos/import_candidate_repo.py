"""`app.import_candidate` rows (EF-22). Repo: pure DB I/O, no commits, no business rules."""

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import ImportCandidate


async def add(
    db: AsyncSession,
    *,
    job_id: UUID,
    film_id: UUID,
    tmdb_id: int,
    title: str,
    headline_release: dict[str, Any] | None,
) -> bool:
    """Propose one film, ticked, and say whether it is new to the list. Caller commits.

    A second proposal of a film already on this job's list is a no-op rather than an error: two
    export rows can resolve to the same film, and the first one's row already says everything
    the second would."""
    inserted = await db.execute(
        insert(ImportCandidate)
        .values(
            job_id=job_id,
            film_id=film_id,
            tmdb_id=tmdb_id,
            title=title,
            headline_release=headline_release,
        )
        .on_conflict_do_nothing(index_elements=["job_id", "film_id"])
        .returning(ImportCandidate.id)
    )
    return inserted.scalar_one_or_none() is not None


async def list_for_job(db: AsyncSession, job_id: UUID) -> list[ImportCandidate]:
    """The job's proposals by title, the order a review list reads in. `id` settles a tie so
    the order is stable between polls."""
    stmt = (
        select(ImportCandidate)
        .where(ImportCandidate.job_id == job_id)
        .order_by(ImportCandidate.title, ImportCandidate.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def selectable_film_ids(
    db: AsyncSession, *, job_id: UUID, film_ids: Collection[UUID]
) -> list[UUID]:
    """Those of `film_ids` that are on this job's list, each once. Every row is followable
    (NEU-1505), so this is what bounds a confirm to the job's own proposals."""
    if not film_ids:
        return []
    stmt = select(ImportCandidate.film_id).where(
        ImportCandidate.job_id == job_id,
        ImportCandidate.film_id.in_(list(film_ids)),
    )
    return list((await db.execute(stmt)).scalars().all())


async def delete_for_jobs(db: AsyncSession, job_ids: Collection[UUID]) -> None:
    """Drop every proposal on these jobs. Caller commits."""
    if not job_ids:
        return
    await db.execute(delete(ImportCandidate).where(ImportCandidate.job_id.in_(list(job_ids))))
