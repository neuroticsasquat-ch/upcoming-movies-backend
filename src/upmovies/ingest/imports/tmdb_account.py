"""The TMDB account import, run as a background job (D-16).

The second half of the approve flow `routers/imports_tmdb.py` starts. That route hands this a
session id the user has just authorized, and this reads their watchlist and their favorites
under it, writes the same two treatments the Letterboxd import writes
(`ingest.imports.apply`), and then **deletes the session at TMDB**.

That last step is the design. The original ticket stored the session id per user, encrypted, so
a later re-sync would not need another approval; the spec replaced it with a one-shot import
because the backend has no encryption dependency and no key convention, and adding both to hold
a credential that can *write* to somebody's TMDB account is real surface for a re-sync nobody
has asked for. Re-importing means re-approving, which is one click. So the delete runs in a
`finally` — the job failing, the user's entitlement lapsing, and a page fetch raising all reach
it — and a delete that itself fails is logged at WARNING rather than failing the job, because
by then the import has already done everything it was asked to do and the session expires at
TMDB on its own.

No resolution step, unlike Letterboxd: TMDB's ids are authoritative, so `unmatched` is empty
except for the one thing that can still go wrong — TMDB answering 404 for a film its own list
points at, which is reported `kind=tmdb_missing`.

Follows the same pipeline contract as the Letterboxd runner: its own session factory, a commit
per row, a throttled heartbeat, and a wrapper that always finalizes."""

import logging
from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import User
from upmovies.app.repos import import_job_repo
from upmovies.config import Settings
from upmovies.db import SessionLocal
from upmovies.ingest.imports.apply import (
    Progress,
    apply_film_people,
    apply_watchlist_film,
    finalize_failed,
)
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.schemas import TMDBMovieSummary

log = logging.getLogger(__name__)

SOURCE = "tmdb"
"""`import_job.source` for these jobs."""

FOLLOW_SOURCE = "tmdb_import"
"""`follow.source` and `watchlist_item.source` for every row this writes (D-10, D-14)."""

MAX_ROWS_PER_LIST = 5_000
"""How much of one TMDB list this will read, matching `letterboxd.MAX_ROWS_PER_FILE`.

The same reasoning and deliberately the same number: at 40 requests / 10 s a list this long is
already ~20 minutes of work, which the job status makes visible, and past it the cost stops
being proportionate to a library anyone actually curates. Unlike the Letterboxd cap this
truncates rather than refuses — there is no upload to hand back, and the alternative to
importing the first five thousand is importing none of them."""


async def run_tmdb_import(
    job_id: UUID, session_id: str, account_id: int, settings: Settings
) -> None:
    """The task the callback route spawns: run the import, delete the TMDB session, and
    finalize the job, whatever happens.

    Nothing awaits this task, so the `except` is the contract: an exception escaping it would
    be swallowed by the event loop and leave the job polling `running` forever — and, worse
    here than for Letterboxd, leave a live session id at TMDB that nothing else will ever
    clean up.

    The client is built outside the `try` and closed in a `finally` of its own rather than with
    `async with`, so that tearing it down cannot change the outcome of the import. Closing a
    connection pool after the job has already finalized `succeeded` has nothing left to say
    about whether the import worked, and inside the `except` it would say the opposite:
    `import_job_repo.finalize` is unconditional, so one failed `aclose` would rewrite a
    finished job as `failed`."""
    client = TMDBClient.from_settings(settings)
    try:
        try:
            await import_tmdb_account(
                session_factory=SessionLocal,
                client=client,
                job_id=job_id,
                session_id=session_id,
                account_id=account_id,
            )
        finally:
            # Before the close, and in a `finally`, so the delete is reached by every way out
            # of the import — success, a crash, or a cancelled task.
            await delete_session(client, session_id)
    except Exception as e:
        log.exception("tmdb import crashed", extra={"job_id": str(job_id)})
        await finalize_failed(job_id, str(e))
    finally:
        await _close(client)


async def _close(client: TMDBClient) -> None:
    """Release the client's connection pool, and never let that be the job's outcome either."""
    try:
        await client.__aexit__()
    except Exception:
        log.warning("tmdb import could not close the TMDB client", exc_info=True)


async def delete_session(client: TMDBClient, session_id: str) -> None:
    """End the TMDB session, and never let that be what fails the import.

    By the time this runs the import has either done its work or already failed for its own
    reason, and neither is improved by replacing the outcome with "could not log out". TMDB
    expires an unused session on its own, so the cost of the WARNING is a credential that dies
    late rather than one that lives forever."""
    try:
        await client.delete_session(session_id)
    except Exception:
        log.warning("tmdb import could not delete the TMDB session", exc_info=True)


async def import_tmdb_account(
    *,
    session_factory: Callable[[], AsyncSession],
    client: TMDBClient,
    job_id: UUID,
    session_id: str,
    account_id: int,
) -> None:
    """Run one account import to completion and finalize the job `succeeded`.

    Raises on anything it cannot handle per row; `run_tmdb_import` is what turns that into a
    `failed` job and what deletes the session. Separate from the wrapper so a test can drive it
    with its own session factory and a respx-mocked client."""
    async with session_factory() as db:
        job = await import_job_repo.get(db, job_id)
        if job is None:
            raise ValueError(f"import job {job_id} does not exist")
        user = await db.get(User, job.user_id)
        if user is None:
            raise ValueError(f"import job {job_id} has no user")
        await import_job_repo.mark_running(db, job_id)
        await db.commit()

        # Both lists up front, because `rows_total` is the denominator the UI's progress bar
        # needs and the callback could not know it: unlike an upload, nothing has read the
        # user's library at the point the job row is created.
        watchlist = _distinct(
            await client.account_watchlist_movies(account_id, session_id, limit=MAX_ROWS_PER_LIST)
        )
        favorites = _distinct(
            await client.account_favorite_movies(account_id, session_id, limit=MAX_ROWS_PER_LIST)
        )
        _warn_if_truncated(job_id, watchlist=len(watchlist), favorites=len(favorites))
        await import_job_repo.set_rows_total(db, job_id, len(watchlist) + len(favorites))
        await db.commit()

        progress = Progress()
        for movie in watchlist:
            if not await apply_watchlist_film(
                db, client, user, movie.id, progress, source=FOLLOW_SOURCE
            ):
                _record_missing(progress, movie)
            await progress.row_done(db, job_id)
        # A favorite that is also on the watchlist gets both treatments (spec §3): the film is
        # what they want telling about, and its people are what the favorite says about them.
        for movie in favorites:
            if not await apply_film_people(
                db, client, user, movie.id, progress, source=FOLLOW_SOURCE
            ):
                _record_missing(progress, movie)
            await progress.row_done(db, job_id)

        await progress.flush(db, job_id)
        await import_job_repo.finalize(db, job_id, status="succeeded")
        await db.commit()


def _record_missing(progress: Progress, movie: TMDBMovieSummary) -> None:
    """Report a film TMDB's own list names but its `/movie/{id}` answers 404 for.

    The title and year come from the list payload rather than from a lookup that has just
    failed — they are what the user will recognise, and the only description of the film left
    once TMDB has deleted the entry."""
    progress.record_unmatched(
        name=movie.title,
        year=movie.release_date.year if movie.release_date else None,
        kind="tmdb_missing",
    )


def _warn_if_truncated(job_id: UUID, *, watchlist: int, favorites: int) -> None:
    """Say so when a list came back at exactly the cap.

    The cap truncates rather than refusing — unlike `letterboxd.MAX_ROWS_PER_FILE` there is no
    upload to hand back, and the alternative to importing the first five thousand is importing
    none of them — so this is the only record that the job read part of a library rather than
    all of it. A `succeeded` job over a truncated read is otherwise indistinguishable from one
    that read everything.

    Counting exactly the cap can be a false positive on a library that happens to be that size
    to the film; at five thousand that costs one log line and no behaviour."""
    for name, count in (("watchlist", watchlist), ("favorites", favorites)):
        if count >= MAX_ROWS_PER_LIST:
            log.warning(
                "tmdb import read only the first %s rows of the %s",
                MAX_ROWS_PER_LIST,
                name,
                extra={"job_id": str(job_id), "list": name},
            )


def _distinct(movies: Sequence[TMDBMovieSummary]) -> list[TMDBMovieSummary]:
    """One entry per TMDB id, first occurrence winning.

    Paging a list somebody is editing can hand the same film back twice — TMDB pages by offset,
    so a removal above the cursor shifts everything down into the next page. Applying a film
    twice writes nothing the second time, but it would count the row twice against `rows_total`
    and make the progress bar the UI polls disagree with itself."""
    seen: dict[int, TMDBMovieSummary] = {}
    for movie in movies:
        seen.setdefault(movie.id, movie)
    return list(seen.values())
