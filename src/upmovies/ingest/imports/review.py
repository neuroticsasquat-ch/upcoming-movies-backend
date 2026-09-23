"""The second phase of an import: the user's answer to the list the job proposed (EF-22).

Both runners stop at `awaiting_review` with an `app.import_candidate` row per matched film and
no follows written, because nothing should be followed until the user has seen what the import
found. This module is everything that happens to that list afterwards, and there are only two
things: the user confirms it, or starts another import instead.

Confirming writes one title follow per id that is a selectable candidate of the job, sets
`follows_created`, moves the job to `succeeded`, and deletes the candidates — all in one
transaction under the job's row lock, so the list cannot be confirmed twice or confirmed after
something else has discarded it.

Starting another import discards the unconfirmed one rather than 409-ing: an abandoned review
list is not "an import in progress" in any sense the user would recognise, and the only thing
a 409 could tell them is to go and find a list they walked away from. The discarded job ends
`failed` with `error = 'superseded'`. Expiring a list on a clock is deliberately not done
(spec §8)."""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import normalise_entity_id
from upmovies.app.models import IMPORT_SUPERSEDED, ImportJob
from upmovies.app.repos import follow_repo, import_candidate_repo, import_job_repo
from upmovies.ingest.imports import runner, tmdb_account

FOLLOW_SOURCES = {
    runner.SOURCE: runner.FOLLOW_SOURCE,
    tmdb_account.SOURCE: tmdb_account.FOLLOW_SOURCE,
}
"""`import_job.source` → the `follow.source` a confirm of that job writes (D-10). Each runner
owns its own pair; this only joins them, because the confirm happens after the runner is gone."""


class ImportJobNotFound(Exception):
    """No such job, or not the caller's — deliberately one answer, as `GET` gives it."""


class ImportNotAwaitingReview(Exception):
    """The job exists and is the caller's, but has no list to confirm: still running, already
    confirmed, failed, or superseded."""


async def confirm(
    db: AsyncSession, *, user_id: UUID, job_id: UUID, film_ids: Collection[UUID]
) -> ImportJob:
    """Follow the films the user kept, finish the job, and return it. Commits.

    Ids that are not selectable candidates of this job — skipped for the alert window, from
    another job, or simply unknown — are ignored rather than refused (the ticket's rule): the
    user answered a list, and the answer is whichever of its ticked rows they sent.

    `follows_created` counts the confirmed rows, as EF-22 words it — every kept film, including
    one the user already followed by hand. That film is followed after the confirm either way,
    and "you now follow these 12" is what the user just did; the existing row is left
    untouched, `source` and all, as every import has always left it (D-15)."""
    job = await import_job_repo.lock_for_user(db, job_id=job_id, user_id=user_id)
    if job is None:
        raise ImportJobNotFound()
    if job.status != "awaiting_review":
        raise ImportNotAwaitingReview()

    kept = await import_candidate_repo.selectable_film_ids(db, job_id=job_id, film_ids=film_ids)
    await follow_repo.create_many_if_absent(
        db,
        user_id=user_id,
        entity_type="title",
        # Through `normalise_entity_id` for the same reason the routes are: `app.follow.entity_id`
        # is polymorphic text with no foreign key, so two spellings of one id are two follow rows
        # that nothing will ever reconcile.
        entity_ids=[normalise_entity_id("title", str(film_id)) for film_id in kept],
        source=FOLLOW_SOURCES[job.source],
    )
    await import_job_repo.mark_confirmed(db, job_id, follows_created=len(kept))
    await import_candidate_repo.delete_for_jobs(db, [job_id])
    await db.commit()
    # The status and counts were written by Core, so the loaded row is stale until re-read.
    await db.refresh(job)
    return job


async def discard_unconfirmed(db: AsyncSession, *, user_id: UUID) -> None:
    """Supersede this user's `awaiting_review` job, if any, so a new import can start. Caller
    commits — in the same transaction as the new job row, so a failed start leaves the old list
    where it was."""
    discarded = await import_job_repo.fail_awaiting_review(
        db, user_id=user_id, error=IMPORT_SUPERSEDED
    )
    await import_candidate_repo.delete_for_jobs(db, discarded)
