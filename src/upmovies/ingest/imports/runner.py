"""The Letterboxd import, run as a background job (D-15).

Why it is a job at all: a Letterboxd export carries no TMDB ids, so every row costs a
`/search/movie`, and every row that matches costs a `/movie/{id}` on top. At the client's
configured 40 requests / 10 s a thousand-row library is minutes of work — far past any request
the user will hold open, and past most proxies' patience too.

That budget is this client's own, not the process's: `RateLimiter` lives inside each
`TMDBClient`, so an import overlapping the daily chain asks TMDB for about twice the intended
rate. The spec's §4 says the limiter is process-wide and that an import merely slows the chain;
it is not, and the accepted-for-v1 reasoning is recorded in AGENTS.md rather than repeated
here. Sharing one limiter across clients is the fix if imports become frequent.

`routers/imports.py` parses and validates the file synchronously, so a bad upload is a 422 the
uploader can act on, then hands the parsed rows here and answers 202 with a job id to poll.

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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import normalise_entity_id
from upmovies.app.models import User, WatchlistDismissal
from upmovies.app.repos import import_job_repo
from upmovies.app.services import follow_service, watchlist_service
from upmovies.catalog.models import Film, FilmCredit
from upmovies.catalog.seed_grade import DIRECTOR_JOB
from upmovies.config import Settings
from upmovies.db import SessionLocal
from upmovies.ingest.imports.letterboxd import (
    LetterboxdExport,
    RatingRow,
    UnmatchedKind,
    WatchlistRow,
)
from upmovies.ingest.tmdb.client import TMDBClient, TMDBNotFound
from upmovies.ingest.tmdb.resolution import ResolvedTitle, resolve
from upmovies.ingest.tmdb.schemas import TMDBCastMember, TMDBCrewMember, TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film, upsert_people

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

IMPORT_TOP_BILLED_ORDER = 2
"""How deep into a rated film's billing a person follow goes, on TMDB's 0-indexed `order`:
slots 0 and 1 (spec §3).

The third cut in the codebase, and deliberately the shallowest. `catalog.seed_grade`'s 5
decides whose filmography the sweep *enumerates*; `derivation_service`'s 3 decides whose
casting earns a **push**. This one decides what a user gets for having liked a film — an
inference from a rating, not a request — and a fifth-billed role in a film somebody enjoyed is
not evidence they want that actor's next project in their timeline. Shallower still would lose
the co-lead."""

CREDITS_FRESH = timedelta(days=7)
"""How recently a watchlist film's credits must have been read for the import to reuse them
instead of re-fetching (spec §3).

Only the watchlist path consults this, because only it writes a `catalog.film` row, and a film
the user is about to be alerted on should carry a current cast, poster and release table. The
ratings path has no equivalent bound on purpose: it wants a director and two names, which do
not change after release, so any credits we hold are as good as fresh ones and a bound there
would buy a re-fetch of the entire back catalogue."""

HEARTBEAT = 2.0
"""Seconds between progress writes. The UI polls every 2 s (spec §1), so writing more often
than this puts rows into the table that nobody reads; writing less often makes an import of a
large library look stalled. The final write is unconditional, so the last rows are never left
un-reported."""


@dataclass
class _Progress:
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


async def run_letterboxd_import(job_id: UUID, export: LetterboxdExport, settings: Settings) -> None:
    """The task the upload route spawns: run the import and finalize the job, whatever happens.

    Mirrors `pipeline_run`'s stage wrappers — the `except` is the contract, not defensiveness.
    Nothing awaits this task, so an exception that escaped it would be swallowed by the event
    loop and the job would poll `running` forever."""
    try:
        async with TMDBClient(
            base_url=settings.tmdb_base_url,
            api_key=settings.tmdb_api_key,
            rate_calls=settings.tmdb_rate_limit_requests,
            rate_window=settings.tmdb_rate_limit_window_seconds,
            retry_max_attempts=settings.tmdb_retry_max_attempts,
        ) as client:
            await import_letterboxd(
                session_factory=SessionLocal, client=client, job_id=job_id, export=export
            )
    except Exception as e:
        log.exception("letterboxd import crashed", extra={"job_id": str(job_id)})
        await _finalize_failed(job_id, str(e))


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

        progress = _Progress()
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
    progress: _Progress,
) -> None:
    """One `watchlist.csv` row: the film in full, a watchlist item, and a title follow.

    The item is written **before** the follow, which is the order `follow_service.follow`'s
    docstring requires and the reverse of the one spec §3's table lists: a title follow derives
    the film it names, so following first would leave a `derived_from_follow` item and this
    row's `source` would never land."""
    film_id = await _resolve_film(db, client, name=row.name, year=row.year)
    if film_id is None:
        progress.record_unmatched(name=row.name, year=row.year, kind="watchlist")
        return

    # D-13: the user already took this film off a derived watchlist, and an import is not a
    # reason to put it back. The follow below is still created — they listed the film, and a
    # follow is a timeline row rather than an alert — and its derivation is blocked by the same
    # dismissal, so the film stays off the watchlist either way.
    dismissed = await db.get(WatchlistDismissal, (user.id, film_id)) is not None
    if not dismissed:
        _, _, _, created = await watchlist_service.add(
            db, user=user, film_id=film_id, source=FOLLOW_SOURCE
        )
        if created:
            progress.watchlist_created += 1

    await _follow(db, user, progress, entity_type="title", entity_id=str(film_id))


async def _import_rating_row(
    db: AsyncSession,
    client: TMDBClient,
    user: User,
    row: RatingRow,
    progress: _Progress,
) -> None:
    """One `ratings.csv` row: person follows for a film rated four stars or better, nothing at
    all for the rest.

    A rating below the cut costs no request and is not unmatched — it was never a candidate, and
    reporting it would bury the titles the user actually has to act on under their whole
    three-star history."""
    if not row.is_promoted:
        return
    people = await _people_for_rated_film(db, client, name=row.name, year=row.year)
    if people is None:
        progress.record_unmatched(name=row.name, year=row.year, kind="rating")
        return
    for person_id in people:
        await _follow(db, user, progress, entity_type="person", entity_id=str(person_id))


async def _follow(
    db: AsyncSession,
    user: User,
    progress: _Progress,
    *,
    entity_type: str,
    entity_id: str,
) -> None:
    """Create one import follow, counting it only if it is new.

    Through `normalise_entity_id` for the same reason the routes are: `app.follow.entity_id` is
    polymorphic text with no foreign key, so two spellings of one id are two follow rows that
    nothing will ever reconcile. `derive=False` — see `follow_service.follow`."""
    _, _, created = await follow_service.follow(
        db,
        user=user,
        entity_type=entity_type,
        entity_id=normalise_entity_id(entity_type, entity_id),
        source=FOLLOW_SOURCE,
        derive=False,
    )
    if created:
        progress.follows_created += 1


async def _resolve_film(
    db: AsyncSession, client: TMDBClient, *, name: str, year: int | None
) -> UUID | None:
    """The `catalog.film` id for a watchlist row, upserting the film if it is absent or stale.
    `None` when the row cannot be placed on a TMDB film at all."""
    hit = await _search(client, name=name, year=year)
    if hit is None:
        return None

    stored = (
        await db.execute(
            select(Film.id, Film.credits_observed_at).where(Film.tmdb_id == hit.tmdb_id)
        )
    ).first()
    if stored is not None and _is_fresh(stored.credits_observed_at):
        return stored.id

    details = await _details(client, hit.tmdb_id)
    if details is None:
        return None
    await upsert_film(db, details)
    await db.commit()
    return (await db.execute(select(Film.id).where(Film.tmdb_id == hit.tmdb_id))).scalar_one()


async def _people_for_rated_film(
    db: AsyncSession, client: TMDBClient, *, name: str, year: int | None
) -> list[int] | None:
    """The TMDB person ids a rated film contributes — its director(s) and top-2 billing — or
    `None` when the row cannot be placed on a film.

    Reads them out of the catalog when we already hold the film's credits and spends a
    `/movie/{id}` only otherwise, which on a re-upload is the difference between minutes and
    seconds."""
    hit = await _search(client, name=name, year=year)
    if hit is None:
        return None

    local = await _local_people(db, hit.tmdb_id)
    if local is not None:
        return local

    details = await _details(client, hit.tmdb_id)
    if details is None:
        return None
    members = _promoted_members(details)
    await upsert_people(db, members)
    await db.commit()
    return list(dict.fromkeys(m.id for m in members))


async def _local_people(db: AsyncSession, tmdb_id: int) -> list[int] | None:
    """The same people read out of `catalog.film_credit`, or `None` if we cannot answer from
    the catalog — the film is absent, or is present but has never had its credits read.

    `credits_observed_at` is the marker `_upsert_credits` sets, so a NULL means "no credits
    were ever written for this film", which is not the same as "this film has no credits" and
    must not be answered with an empty list."""
    stored = (
        await db.execute(select(Film.id, Film.credits_observed_at).where(Film.tmdb_id == tmdb_id))
    ).first()
    if stored is None or stored.credits_observed_at is None:
        return None

    rows = (
        await db.execute(
            select(
                FilmCredit.person_id,
                FilmCredit.credit_type,
                FilmCredit.job,
                FilmCredit.credit_order,
            )
            .where(FilmCredit.film_id == stored.id)
            .order_by(FilmCredit.credit_order, FilmCredit.person_id)
        )
    ).all()
    directors = [r.person_id for r in rows if r.credit_type == "crew" and r.job == DIRECTOR_JOB]
    billed = [
        r.person_id
        for r in rows
        if r.credit_type == "cast"
        and r.credit_order is not None
        and r.credit_order < IMPORT_TOP_BILLED_ORDER
    ]
    return list(dict.fromkeys([*directors, *billed]))


def _promoted_members(
    details: TMDBMovieDetails,
) -> Sequence[TMDBCastMember | TMDBCrewMember]:
    """The director(s) and top-2 billed cast of a rated film, directors first.

    Directors first so the follow rows a user ends up with are ordered by how much the credit
    says about why they liked the film, and so the order does not depend on TMDB's."""
    if details.credits is None:
        return []
    directors: list[TMDBCrewMember] = [m for m in details.credits.crew if m.job == DIRECTOR_JOB]
    billed: list[TMDBCastMember] = [
        m for m in details.credits.cast if m.order is not None and m.order < IMPORT_TOP_BILLED_ORDER
    ]
    billed.sort(key=lambda m: (m.order or 0, m.id))
    return _dedupe([*directors, *billed])


def _dedupe(
    members: Iterable[TMDBCastMember | TMDBCrewMember],
) -> list[TMDBCastMember | TMDBCrewMember]:
    """First entry per TMDB id wins — an actor who also directed is one person."""
    seen: dict[int, TMDBCastMember | TMDBCrewMember] = {}
    for member in members:
        seen.setdefault(member.id, member)
    return list(seen.values())


async def _search(client: TMDBClient, *, name: str, year: int | None) -> ResolvedTitle | None:
    """One `/search/movie`, matched by `ingest.tmdb.resolution`. A row with no year is not
    searched at all — no rule can place it, so the request would be spent to learn nothing."""
    if year is None:
        return None
    hits = await client.search_movie(name, year)
    return resolve(hits, name=name, year=year)


async def _details(client: TMDBClient, tmdb_id: int) -> TMDBMovieDetails | None:
    """`/movie/{id}`, or `None` if TMDB no longer has it.

    A 404 here is the search index being ahead of the entry it points at, which is a property
    of that one title and not of the import: the row is reported unmatched, exactly as an
    unplaceable title is, and the remaining rows carry on. Every other failure propagates and
    fails the job — see the module docstring."""
    try:
        return await client.movie_details(tmdb_id)
    except TMDBNotFound:
        log.info("letterboxd import: TMDB has no entry at %s", tmdb_id)
        return None


def _is_fresh(credits_observed_at: datetime | None) -> bool:
    return (
        credits_observed_at is not None and datetime.now(UTC) - credits_observed_at <= CREDITS_FRESH
    )


async def _finalize_failed(job_id: UUID, error: str) -> None:
    async with SessionLocal() as db:
        await import_job_repo.finalize(db, job_id, status="failed", error=error)
        await db.commit()
