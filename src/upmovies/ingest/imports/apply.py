"""What an import writes once it knows which TMDB film a row means — shared by the Letterboxd
import (D-15) and the TMDB account import (D-16).

The two imports differ entirely in how they *find* a film and not at all in what they do with
one. Letterboxd gives a title and a year and has to spend a `/search/movie` guessing at the id;
TMDB gives the id. Past that point both want the same treatment, so it lives here and the
resolution stays in each runner.

**There is one treatment now** (EF-20). `apply_film_people` — the director and top-2 billing of
a rated or favorited film, as person follows — is gone, with the `ratings.csv` path and the
favorites list that fed it. A follow is binary (EF-1), so a follow inferred from a four-star
rating in 2019 would push on every credit change of somebody the user once enjoyed; nobody
asked for that, and NEU-1432's migration has already deleted the rows the path wrote.

**It proposes; it does not follow** (EF-22). Each matched film becomes an
`app.import_candidate` row, and the job stops at `awaiting_review` for the user to confirm the
list — `ingest.imports.review` owns that half, and is where the follows are written.

**And it only offers films that can still deliver something** (EF-21). A watchlisted film
outside the alert window — called off, or released longer ago than the provider poll keeps
looking — is upserted and listed, but unticked with `skip_reason = 'outside_window'`, so the
user can see what the import declined rather than wondering where the title went.

Also here: `Progress`, the running totals both runners keep, because the counts it reports are
incremented inside `propose_film`."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import headline_release_out
from upmovies.app.repos import import_candidate_repo, import_job_repo
from upmovies.catalog.headline_release import headline_releases
from upmovies.catalog.models import Film
from upmovies.catalog.queries import alert_window_clause
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.ingest.tmdb.client import TMDBClient, TMDBNotFound
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film

log = logging.getLogger(__name__)

UnmatchedKind = Literal["watchlist", "rating", "tmdb_missing", "outside_window"]
"""Why a title the user listed produced no follow — the `kind` stored on each row of
`app.import_job.unmatched`.

- `watchlist` — no `/search/movie` rule would place the title (Letterboxd only).
- `tmdb_missing` — TMDB has deleted the entry its own list still points at. The TMDB import's
  only resolution failure, because the ids there are authoritative (D-16); telling the user
  which list it was on would not help them find something that is gone.
- `outside_window` — **historical**. NEU-1448 reported a matched film outside the alert window
  (EF-21) here; since EF-22 it is an `app.import_candidate` row with that `skip_reason`
  instead, because it was matched and the review list is where matched films go.
  `app.dto.ImportJobOut` drops rows of this kind on the way out, so the jobs that ran in
  between still poll without a 500 and without a matched film reading as unmatched.
- `rating` — **historical** on the same terms. The ratings path is deleted (EF-20) and nothing
  writes this any more, but the column is JSONB and jobs that ran before this shipped still
  hold rows carrying it. Kept so polling one of those does not 500 on its own report."""

CREDITS_FRESH = timedelta(days=7)
"""How recently a watchlisted film's credits must have been read for an import to reuse them
instead of re-fetching (NEU-1356 §3).

A film the user is about to be alerted on should carry a current cast, poster and release
table. **Note what this bound does not buy**: inside it the window (EF-21) is read off the
stored row, so a film TMDB called off in the last seven days is still followed. That is the
trade the bound was always making, and it is a small one here — the 365-day date half of the
window is immune to a week of drift, and only a very recent cancellation slips through, which
the next daily ingest corrects."""

HEARTBEAT = 2.0
"""Seconds between progress writes. The UI polls every 2 s (NEU-1356 §1), so writing more often
than this puts rows into the table that nobody reads; writing less often makes an import of a
large library look stalled. The final write is unconditional, so the last rows are never left
un-reported."""

WatchlistOutcome = Literal["proposed", "outside_window", "tmdb_missing"]
"""What `propose_film` did with one listed film. `outside_window` is still a candidate, just an
unticked one, so only `tmdb_missing` leaves the runner anything to report — and it reports that
its own way, Letterboxd naming the list and TMDB the cause."""


@dataclass
class Progress:
    """The running totals, written through to the job row on a throttle.

    Held in memory and written whole (`import_job_repo.record_progress`) rather than
    incremented in SQL: this is the only writer, so the in-memory value is authoritative, and
    a throttled absolute write cannot drift the way a skipped or repeated increment can."""

    rows_done: int = 0
    watchlist_created: int = 0
    follows_created: int = 0
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    _last_write: float = field(default_factory=lambda: monotonic())

    def record_unmatched(self, *, name: str, year: int | None, kind: UnmatchedKind) -> None:
        self.unmatched.append({"name": name, "year": year, "kind": kind})

    async def row_done(self, db: AsyncSession, job_id: UUID, *, force: bool = False) -> None:
        self.rows_done += 1
        if force or monotonic() - self._last_write >= HEARTBEAT:
            await self.flush(db, job_id)

    async def flush(self, db: AsyncSession, job_id: UUID) -> None:
        await import_job_repo.record_progress(
            db,
            job_id,
            rows_done=self.rows_done,
            watchlist_created=self.watchlist_created,
            follows_created=self.follows_created,
            unmatched=self.unmatched,
        )
        await db.commit()
        self._last_write = monotonic()


async def propose_film(
    db: AsyncSession,
    client: TMDBClient,
    job_id: UUID,
    tmdb_id: int,
    progress: Progress,
) -> WatchlistOutcome:
    """The only treatment an import has: the film in full, and a candidate for the review list
    (EF-22) — ticked if the film is still inside the alert window, unticked with a reason if it
    is not (EF-21). Committed, so the per-row contract holds for the candidate as it does for
    the film.

    **No follow is written here.** The user has not seen the list yet; `ingest.imports.review`
    writes the follows for the rows they confirm.

    **The film is upserted either way** (EF-21). The window is read off the stored row, so the
    upsert has to happen first; and a film somebody listed is worth holding in the catalog even
    when this import will not offer it — the next import, or a manual follow, finds it already
    there. What the window decides is the *tick*, not the row."""
    film_id = await film_id_for(db, client, tmdb_id)
    if film_id is None:
        return "tmdb_missing"

    in_window = await in_alert_window(db, film_id)
    title = (await db.execute(select(Film.title).where(Film.id == film_id))).scalar_one()
    headline = (await headline_releases(db, [film_id], today=datetime.now(UTC).date())).get(film_id)
    out = headline_release_out(headline)
    added = await import_candidate_repo.add(
        db,
        job_id=job_id,
        film_id=film_id,
        tmdb_id=tmdb_id,
        title=title,
        headline_release=None if out is None else out.model_dump(mode="json"),
        skip_reason=None if in_window else "outside_window",
    )
    await db.commit()
    if not in_window:
        return "outside_window"
    if added:
        # `watchlist_created` counts the films the import offers — the ticked rows, each once —
        # and `follows_created` stays at zero until the confirm sets it to the rows the user
        # kept. The two part company here (EF-22): a list of forty is not forty follows until
        # somebody says so.
        progress.watchlist_created += 1
    return "proposed"


async def in_alert_window(db: AsyncSession, film_id: UUID) -> bool:
    """Whether a film can still deliver anything to a follow (EF-21, D-46): any status but
    `Canceled`, and a primary release date in the future or within
    `PROVIDER_POLL_MAX_AGE_DAYS` of today.

    `catalog.queries.alert_window_clause` asked of one row rather than re-spelled here, because
    a second spelling of the window is exactly how it drifts from the provider poll it is
    supposed to agree with.

    A re-read rather than a value carried out of the upsert: `film_id_for` returns early for a
    film whose credits are fresh without ever loading its status, and a freshly upserted film's
    ORM state is not what Core wrote (CLAUDE.md's `populate_existing` rule). One indexed lookup
    per row is the cheap way to be right."""
    settings = get_settings()
    stmt = select(Film.id).where(
        Film.id == film_id,
        alert_window_clause(
            today=datetime.now(UTC).date(), max_age_days=settings.provider_poll_max_age_days
        ),
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def film_id_for(db: AsyncSession, client: TMDBClient, tmdb_id: int) -> UUID | None:
    """The `catalog.film` id for a TMDB id, upserting the film if it is absent or stale.
    `None` when TMDB no longer has it."""
    stored = (
        await db.execute(select(Film.id, Film.credits_observed_at).where(Film.tmdb_id == tmdb_id))
    ).first()
    if stored is not None and _is_fresh(stored.credits_observed_at):
        return stored.id

    details = await fetch_details(client, tmdb_id)
    if details is None:
        return None
    await upsert_film(db, details)
    await db.commit()
    return (await db.execute(select(Film.id).where(Film.tmdb_id == tmdb_id))).scalar_one()


async def fetch_details(client: TMDBClient, tmdb_id: int) -> TMDBMovieDetails | None:
    """`/movie/{id}`, or `None` if TMDB no longer has it.

    A 404 here is a list being ahead of the entry it points at — the search index for
    Letterboxd, the user's own TMDB list for the account import — which is a property of that
    one title and not of the run: the row is reported unmatched and the remaining rows carry
    on. Every other failure propagates and fails the job."""
    try:
        return await client.movie_details(tmdb_id)
    except TMDBNotFound:
        log.info("import: TMDB has no entry at %s", tmdb_id)
        return None


def _is_fresh(credits_observed_at: datetime | None) -> bool:
    return (
        credits_observed_at is not None and datetime.now(UTC) - credits_observed_at <= CREDITS_FRESH
    )


async def finalize_failed(job_id: UUID, error: str) -> None:
    """Move a job to `failed` on its own session, for a runner whose own session is gone.

    Its own session factory rather than the runner's, because the crash this answers may be the
    runner's session dying — reusing it would fail the write that records the failure.

    The candidates the run had proposed go with it: a failed job is never confirmed, and a
    half-read library offered as a list would look like the whole of one."""
    async with SessionLocal() as db:
        await import_job_repo.finalize(db, job_id, status="failed", error=error)
        await import_candidate_repo.delete_for_jobs(db, [job_id])
        await db.commit()
