"""The Letterboxd import, run as a background job (D-15).

Why it is a job at all: a Letterboxd export carries no TMDB ids, so every row costs a
`/search/movie`, and every row that matches costs a `/movie/{id}` on top. At the client's
configured 40 requests / 10 s a thousand-row library is minutes of work — far past any request
the user will hold open, and past most proxies' patience too.

`routers/imports.py` parses and validates the file synchronously, so a bad upload is a 422 the
uploader can act on, then hands the parsed rows here and answers 202 with a job id to poll.

What is left in this module is the half that is Letterboxd's: turning a title and a year into a
TMDB id, and deciding which rows are candidates at all. What a matched film then *becomes* —
the watchlist item and the title follow, or the person follows of a promoted rating — is in
`ingest.imports.apply`, shared with the TMDB account import (D-16), which arrives at the same
two treatments from ids it does not have to guess.

**Rated films contribute people only** (spec §3, and the Problem section's reasoning). The
catalog is the upcoming-film spine: a film someone rated four stars in 2019 has nothing left to
announce, so it is fetched for its director and top billing and then discarded — no
`catalog.film` row. Watchlist films *are* upserted in full, because they feed the provider poll
set (D-27) and are the films the user is asking to be told about.

Follows the pipeline contract the rest of `ingest` keeps (CLAUDE.md): its own session factory,
a commit per row so a crash keeps the rows already done, a time-throttled heartbeat the UI
polls, and a wrapper that always finalizes — `failed` with the error on an unexpected crash.
The one place it deliberately does *not* isolate per item is that same crash: a row that raises
something other than a TMDB 404 stops the job rather than being counted and skipped, because at
that point the likely cause is the whole import's (a dead client, a lost database) and burning
the remaining thousand rows against it helps nobody."""

import logging
from collections.abc import Callable
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
from upmovies.ingest.imports.letterboxd import LetterboxdExport, RatingRow, WatchlistRow
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.resolution import ResolvedTitle, resolve

log = logging.getLogger(__name__)

SOURCE = "letterboxd"
"""`import_job.source` for these jobs."""

FOLLOW_SOURCE = "letterboxd_import"
"""`follow.source` and `watchlist_item.source` for every row this writes (D-10, D-14).

Note the second half: `NEU-1356-letterboxd-import.md` §3's table says the watchlist item is
written `source=manual`, and that predates NEU-1349 defining the column's values. The model's
CHECK enumerates `letterboxd_import` and says in as many words that D-15 writes it, which is
both the later statement and the truthful one — `manual` means the user clicked the button.
Nothing branches on the difference (only `derived_from_follow` changes behaviour, by making a
removal a dismissal), so this is a naming correction, not a behavioural one."""


async def run_letterboxd_import(job_id: UUID, export: LetterboxdExport, settings: Settings) -> None:
    """The task the upload route spawns: run the import and finalize the job, whatever happens.

    Mirrors `pipeline_run`'s stage wrappers — the `except` is the contract, not defensiveness.
    Nothing awaits this task, so an exception that escaped it would be swallowed by the event
    loop and the job would poll `running` forever."""
    try:
        async with TMDBClient.from_settings(settings) as client:
            await import_letterboxd(
                session_factory=SessionLocal, client=client, job_id=job_id, export=export
            )
    except Exception as e:
        log.exception("letterboxd import crashed", extra={"job_id": str(job_id)})
        await finalize_failed(job_id, str(e))


async def import_letterboxd(
    *,
    session_factory: Callable[[], AsyncSession],
    client: TMDBClient,
    job_id: UUID,
    export: LetterboxdExport,
) -> None:
    """Run one import to completion and finalize the job `succeeded`.

    Raises on anything it cannot handle per row; `run_letterboxd_import` is what turns that
    into a `failed` job. Separate from the wrapper so a test can drive it with its own session
    factory and a respx-mocked client."""
    async with session_factory() as db:
        job = await import_job_repo.get(db, job_id)
        if job is None:
            raise ValueError(f"import job {job_id} does not exist")
        user = await db.get(User, job.user_id)
        if user is None:
            raise ValueError(f"import job {job_id} has no user")
        await import_job_repo.mark_running(db, job_id)
        await db.commit()

        progress = Progress()
        for watchlist_row in export.watchlist:
            await _import_watchlist_row(db, client, user, watchlist_row, progress)
            await progress.row_done(db, job_id)
        for rating_row in export.ratings:
            await _import_rating_row(db, client, user, rating_row, progress)
            await progress.row_done(db, job_id)

        await progress.flush(db, job_id)
        await import_job_repo.finalize(db, job_id, status="succeeded")
        await db.commit()


async def _import_watchlist_row(
    db: AsyncSession,
    client: TMDBClient,
    user: User,
    row: WatchlistRow,
    progress: Progress,
) -> None:
    """One `watchlist.csv` row: the film in full, a watchlist item, and a title follow."""
    hit = await _search(client, name=row.name, year=row.year)
    if hit is None or not await apply_watchlist_film(
        db, client, user, hit.tmdb_id, progress, source=FOLLOW_SOURCE
    ):
        progress.record_unmatched(name=row.name, year=row.year, kind="watchlist")


async def _import_rating_row(
    db: AsyncSession,
    client: TMDBClient,
    user: User,
    row: RatingRow,
    progress: Progress,
) -> None:
    """One `ratings.csv` row: person follows for a film rated four stars or better, nothing at
    all for the rest.

    A rating below the cut costs no request and is not unmatched — it was never a candidate, and
    reporting it would bury the titles the user actually has to act on under their whole
    three-star history."""
    if not row.is_promoted:
        return
    hit = await _search(client, name=row.name, year=row.year)
    if hit is None or not await apply_film_people(
        db, client, user, hit.tmdb_id, progress, source=FOLLOW_SOURCE
    ):
        progress.record_unmatched(name=row.name, year=row.year, kind="rating")


async def _search(client: TMDBClient, *, name: str, year: int | None) -> ResolvedTitle | None:
    """One `/search/movie`, matched by `ingest.tmdb.resolution`. A row with no year is not
    searched at all — no rule can place it, so the request would be spent to learn nothing."""
    if year is None:
        return None
    hits = await client.search_movie(name, year)
    return resolve(hits, name=name, year=year)
