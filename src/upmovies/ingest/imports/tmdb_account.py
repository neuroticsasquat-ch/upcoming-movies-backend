"""The TMDB account import, run as a background job (D-16).

The second half of the approve flow `routers/imports_tmdb.py` starts. That route hands this a
session id the user has just authorized, and this reads **their watchlist** under it, writes
the same treatment the Letterboxd import writes (`ingest.imports.apply`) — candidates for the
review list, no follows until the user confirms it (EF-22) — and then **deletes the session at
TMDB**.

**The favorites list is not read** (EF-20). It used to buy person follows for each favorite's
director and top-2 billing, and a follow is binary now (EF-1) — so a film somebody favorited
years ago would push every credit change of its cast at them. The approve flow's scopes are
unchanged: TMDB grants one session per approval and does not scope it per list, so there is
nothing narrower to ask the user for.

That last step is the design. The original ticket stored the session id per user, encrypted, so
a later re-sync would not need another approval; the spec replaced it with a one-shot import
because the backend has no encryption dependency and no key convention, and adding both to hold
a credential that can *write* to somebody's TMDB account is real surface for a re-sync nobody
has asked for. Re-importing means re-approving, which is one click. So the delete runs in a
`finally` — the job failing, the user's entitlement lapsing, and a page fetch raising all reach
it — and a delete that itself fails is logged at WARNING rather than failing the job, because
by then the import has already done everything it was asked to do and the session expires at
TMDB on its own.

No resolution step, unlike Letterboxd: TMDB's ids are authoritative, so the only row that can
reach the report names a cause rather than a list — TMDB answering 404 for a film its own list
points at (`kind=tmdb_missing`). A film the alert window has closed on (EF-21) is on the review
list instead, unticked with its reason.

Follows the same pipeline contract as the Letterboxd runner: its own session factory, a commit
per row, a throttled heartbeat, and a wrapper that always finalizes."""

import logging
from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.repos import import_job_repo
from upmovies.config import Settings
from upmovies.db import SessionLocal
from upmovies.ingest.imports.apply import Progress, finalize_failed, propose_film
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.schemas import TMDBMovieSummary

log = logging.getLogger(__name__)

SOURCE = "tmdb"
"""`import_job.source` for these jobs."""

FOLLOW_SOURCE = "tmdb_import"
"""`follow.source` for every row a confirm of one of these jobs writes (D-10, D-14)."""

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
    """Run one account import to completion and leave the job `awaiting_review` (EF-22).

    Raises on anything it cannot handle per row; `run_tmdb_import` is what turns that into a
    `failed` job and what deletes the session. Separate from the wrapper so a test can drive it
    with its own session factory and a respx-mocked client."""
    async with session_factory() as db:
        job = await import_job_repo.get(db, job_id)
        if job is None:
            raise ValueError(f"import job {job_id} does not exist")
        await import_job_repo.mark_running(db, job_id)
        await db.commit()

        # The list up front, because `rows_total` is the denominator the UI's progress bar
        # needs and the callback could not know it: unlike an upload, nothing has read the
        # user's library at the point the job row is created.
        watchlist = _distinct(
            await client.account_watchlist_movies(account_id, session_id, limit=MAX_ROWS_PER_LIST)
        )
        _warn_if_truncated(job_id, watchlist=len(watchlist))
        await import_job_repo.set_rows_total(db, job_id, len(watchlist))
        await db.commit()

        progress = Progress()
        for movie in watchlist:
            outcome = await propose_film(db, client, job_id, movie.id, progress)
            if outcome == "tmdb_missing":
                _record_missing(progress, movie)
            await progress.row_done(db, job_id)

        await progress.flush(db, job_id)
        await import_job_repo.mark_awaiting_review(db, job_id)
        await db.commit()


def _record_missing(progress: Progress, movie: TMDBMovieSummary) -> None:
    """Report a film on the account's watchlist that TMDB's own `/movie/{id}` answers 404 for.

    The title and year come from the list payload, because for a deleted entry they are the only
    description of the film left."""
    progress.record_unmatched(
        name=movie.title,
        year=movie.release_date.year if movie.release_date else None,
        kind="tmdb_missing",
    )


def _warn_if_truncated(job_id: UUID, *, watchlist: int) -> None:
    """Say so when the watchlist came back at exactly the cap.

    The cap truncates rather than refusing — unlike `letterboxd.MAX_ROWS_PER_FILE` there is no
    upload to hand back, and the alternative to importing the first five thousand is importing
    none of them — so this is the only record that the job read part of a library rather than
    all of it. A `succeeded` job over a truncated read is otherwise indistinguishable from one
    that read everything.

    Counting exactly the cap can be a false positive on a library that happens to be that size
    to the film; at five thousand that costs one log line and no behaviour."""
    if watchlist >= MAX_ROWS_PER_LIST:
        log.warning(
            "tmdb import read only the first %s rows of the watchlist",
            MAX_ROWS_PER_LIST,
            extra={"job_id": str(job_id), "list": "watchlist"},
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
