"""The Letterboxd import, run as a background job (D-15).

Why it is a job at all: a Letterboxd export carries no TMDB ids, so every row costs a
`/search/movie`, and every row that matches costs a `/movie/{id}` on top. At the client's
configured 40 requests / 10 s a thousand-row library is minutes of work — far past any request
the user will hold open, and past most proxies' patience too.

`routers/imports.py` parses and validates the file synchronously, so a bad upload is a 422 the
uploader can act on, then hands the parsed rows here and answers 202 with a job id to poll.

What is left in this module is the half that is Letterboxd's: turning a title and a year into a
TMDB id. What a matched film then *becomes* — the film in full and a candidate on the review
list, if it is still inside the alert window — is in `ingest.imports.apply`, shared with
the TMDB account import (D-16), which arrives at the same treatment from ids it does not have to
guess. **The run writes no follows** (EF-22): it ends at `awaiting_review`, and the user's
confirm (`ingest.imports.review`) is what follows the films they kept.

**Only the watchlist is read** (EF-20). The `ratings.csv` half of this runner is gone with the
follows it used to infer: a follow is binary now (EF-1), and a four-star rating is not a
request to hear about everything that actor does next.

Follows the pipeline contract the rest of `ingest` keeps (CLAUDE.md): its own session factory,
a commit per row so a crash keeps the rows already done, a time-throttled heartbeat the UI
polls, and a wrapper that always finalizes — `failed` with the error on an unexpected crash.
The one place it deliberately does *not* isolate per item is that same crash: a row that raises
something other than a TMDB 404 stops the job rather than being counted and skipped, because at
that point the likely cause is the whole import's (a dead client, a lost database) and burning
the remaining thousand rows against it helps nobody.

**Old films cost nothing** (NEU-1505, D-1505.6). A film outside the alert window is declined
at the first point its date is known: before the search when the export's `Year` alone rules it
out, and before `/movie/{id}` when the accepted hit's date does. Either way it is reported as
`outside_window` and never reaches the catalog."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.repos import import_job_repo
from upmovies.config import Settings, get_settings
from upmovies.db import SessionLocal
from upmovies.ingest.imports.apply import Progress, date_in_window, finalize_failed, propose_film
from upmovies.ingest.imports.letterboxd import LetterboxdExport, WatchlistRow
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.resolution import FESTIVAL_YEAR_SLACK, ResolvedTitle, resolve

log = logging.getLogger(__name__)

SOURCE = "letterboxd"
"""`import_job.source` for these jobs."""

FOLLOW_SOURCE = "letterboxd_import"
"""`follow.source` for every row a confirm of one of these jobs writes (D-10, D-14).

Not `manual`, which `NEU-1356-letterboxd-import.md` §3's table asked for before NEU-1349
defined the column's values: `app.follow`'s CHECK enumerates `letterboxd_import` and says in
as many words that D-15 writes it, and `manual` means the user clicked the button."""


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
    """Run one import to completion and leave the job `awaiting_review` (EF-22).

    Raises on anything it cannot handle per row; `run_letterboxd_import` is what turns that
    into a `failed` job. Separate from the wrapper so a test can drive it with its own session
    factory and a respx-mocked client."""
    async with session_factory() as db:
        job = await import_job_repo.get(db, job_id)
        if job is None:
            raise ValueError(f"import job {job_id} does not exist")
        await import_job_repo.mark_running(db, job_id)
        await db.commit()

        progress = Progress()
        for watchlist_row in export.watchlist:
            await _import_watchlist_row(db, client, job_id, watchlist_row, progress)
            await progress.row_done(db, job_id)

        await progress.flush(db, job_id)
        await import_job_repo.mark_awaiting_review(db, job_id)
        await db.commit()


async def _import_watchlist_row(
    db: AsyncSession,
    client: TMDBClient,
    job_id: UUID,
    row: WatchlistRow,
    progress: Progress,
) -> None:
    """One `watchlist.csv` row: the film in full, and a candidate for the review list, if it is
    still inside the alert window (EF-21, EF-22).

    Both failures are reported under `kind="watchlist"`, and deliberately: a title this could
    not place and a title TMDB has since deleted are the same fact to a Letterboxd uploader —
    the row is in their export, and it is not on the list. A film outside the window is
    `outside_window` wherever it is caught — by its year, by its hit's date, or by the stored
    row's status — because to the user it is one fact: too old to follow (NEU-1505).

    The name and year are the export's, verbatim, rather than the catalog's: the user is going
    to look for this row in their own file."""
    if row.year is not None and _year_outside_window(row.year):
        progress.record_unmatched(name=row.name, year=row.year, kind="outside_window")
        return

    hit = await _search(client, name=row.name, year=row.year)
    if hit is None:
        progress.record_unmatched(name=row.name, year=row.year, kind="watchlist")
        return
    if not date_in_window(hit.release_date):
        progress.record_unmatched(name=row.name, year=row.year, kind="outside_window")
        return

    outcome = await propose_film(db, client, job_id, hit.tmdb_id, progress)
    if outcome == "tmdb_missing":
        progress.record_unmatched(name=row.name, year=row.year, kind="watchlist")
    elif outcome == "outside_window":
        progress.record_unmatched(name=row.name, year=row.year, kind="outside_window")


def _year_outside_window(year: int) -> bool:
    """Whether no film the search could accept for `year` can be inside the alert window, so
    the row is declined with no request at all (NEU-1505, D-1505.6).

    `resolve` accepts a hit only within `FESTIVAL_YEAR_SLACK` of the export's year, so the
    latest primary date any acceptable hit can carry is 31 December of `year + slack`. When
    that year is before the one the window starts in, nothing the search returns could pass
    `date_in_window`, and the search would be spent to learn nothing."""
    window_start = datetime.now(UTC).date() - timedelta(
        days=get_settings().provider_poll_max_age_days
    )
    return year + FESTIVAL_YEAR_SLACK < window_start.year


async def _search(client: TMDBClient, *, name: str, year: int | None) -> ResolvedTitle | None:
    """One `/search/movie`, matched by `ingest.tmdb.resolution`. A row with no year is not
    searched at all — no rule can place it, so the request would be spent to learn nothing."""
    if year is None:
        return None
    hits = await client.search_movie(name, year)
    return resolve(hits, name=name, year=year)
