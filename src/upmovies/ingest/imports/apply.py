"""What an import writes once it knows which TMDB film a row means — shared by the Letterboxd
import (D-15) and the TMDB account import (D-16).

The two imports differ entirely in how they *find* a film and not at all in what they do with
one. Letterboxd gives a title and a year and has to spend a `/search/movie` guessing at the id;
TMDB gives the id. Past that point both want the same two treatments, and the spec for the
second says so in as many words — "the NEU-1356 watchlist path exactly", "the NEU-1356 ratings
path exactly". So the two treatments live here and the resolution stays in each runner:

- `apply_watchlist_film` — the film in full and a title follow. For a film the user is asking
  to be told about.
- `apply_film_people` — the director and top-2 billing as person follows, and no
  `catalog.film` row. For a film the user is telling us about their taste with.

The `source` each writes is the caller's, because it is the one thing that genuinely differs:
`letterboxd_import` or `tmdb_import` on every row (D-10).

Also here: `Progress`, the running totals both runners keep, because the counts it reports are
incremented inside these two functions."""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import normalise_entity_id
from upmovies.app.models import User
from upmovies.app.repos import import_job_repo
from upmovies.app.services import follow_service
from upmovies.catalog.models import Film, FilmCredit
from upmovies.catalog.seed_grade import DIRECTOR_JOB
from upmovies.db import SessionLocal
from upmovies.ingest.tmdb.client import TMDBClient, TMDBNotFound
from upmovies.ingest.tmdb.schemas import TMDBCastMember, TMDBCrewMember, TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film, upsert_people

log = logging.getLogger(__name__)

UnmatchedKind = Literal["watchlist", "rating", "tmdb_missing"]
"""Why a title the user listed produced nothing.

The first two are Letterboxd's, and name the *list* the unplaced row came from, because that is
what the user goes looking in. `tmdb_missing` is the TMDB import's only failure mode and names
the *cause* instead: those ids are authoritative, so a row fails there only when TMDB has
deleted the entry its own list still points at, and telling the user which list it was on would
not help them find something that is gone."""

IMPORT_TOP_BILLED_ORDER = 2
"""How deep into a film's billing a person follow goes, on TMDB's 0-indexed `order`: slots 0
and 1 (NEU-1356 §3).

The second cut in the codebase, and deliberately the shallower. `catalog.seed_grade`'s 5
decides whose filmography the sweep *enumerates*. This one decides what a user gets for having
liked a film — an inference from a rating or a favorite, not a request — and a fifth-billed
role in a film somebody enjoyed is not evidence they want that actor's next project in their
timeline.
Shallower still would lose the co-lead."""

CREDITS_FRESH = timedelta(days=7)
"""How recently a watchlisted film's credits must have been read for an import to reuse them
instead of re-fetching (NEU-1356 §3).

Only the watchlist path consults this, because only it writes a `catalog.film` row, and a film
the user is about to be alerted on should carry a current cast, poster and release table. The
people path has no equivalent bound on purpose: it wants a director and two names, which do not
change after release, so any credits we hold are as good as fresh ones and a bound there would
buy a re-fetch of the entire back catalogue."""

HEARTBEAT = 2.0
"""Seconds between progress writes. The UI polls every 2 s (NEU-1356 §1), so writing more often
than this puts rows into the table that nobody reads; writing less often makes an import of a
large library look stalled. The final write is unconditional, so the last rows are never left
un-reported."""


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


async def apply_watchlist_film(
    db: AsyncSession,
    client: TMDBClient,
    user: User,
    tmdb_id: int,
    progress: Progress,
    *,
    source: str,
) -> bool:
    """The watchlist treatment for one film: the film in full and a title follow. `False` when
    TMDB no longer has the film, so the caller can report it.

    One row, not two (M8). A title follow *is* the film being on the watchlist now, so the
    watchlist item this used to write first — and the ordering rule that went with it — are
    gone with the table.

    **A mute the user has on file is left alone.** They listed the film, so the follow is
    created; they silenced it, so it stays silenced, and `GET /me/watchlist` shows it as
    `muted: true` for them to undo. The old code skipped the *item* on a dismissal, which was
    the same judgement about the same two facts — an import is not a reason to un-silence
    something — and the film would end up off the watchlist either way."""
    film_id = await film_id_for(db, client, tmdb_id)
    if film_id is None:
        return False

    created = await follow_entity(
        db, user, progress, entity_type="title", entity_id=str(film_id), source=source
    )
    if created:
        # Counted as a watchlist film rather than a follow, because this is the count the
        # onboarding screen renders as "N watchlist films" (`import_job.watchlist_created`)
        # and a title follow is what a watchlist row has become. `follows_created` keeps
        # meaning the person follows the taste half inferred.
        progress.watchlist_created += 1
    return True


async def apply_film_people(
    db: AsyncSession,
    client: TMDBClient,
    user: User,
    tmdb_id: int,
    progress: Progress,
    *,
    source: str,
) -> bool:
    """The people treatment for one film: person follows for its director and top-2 billing,
    and no `catalog.film` row. `False` when TMDB no longer has the film.

    The catalog is the upcoming-film spine: a film someone rated four stars in 2019, or has had
    on their favorites since, has nothing left to announce, so it is fetched for its director
    and top billing and then discarded."""
    people = await people_for(db, client, tmdb_id)
    if people is None:
        return False
    for person_id in people:
        if await follow_entity(
            db, user, progress, entity_type="person", entity_id=str(person_id), source=source
        ):
            progress.follows_created += 1
    return True


async def follow_entity(
    db: AsyncSession,
    user: User,
    progress: Progress,
    *,
    entity_type: str,
    entity_id: str,
    source: str,
) -> bool:
    """Create one import follow and say whether it is new.

    The count belongs to the caller, not here: the same call writes a *watchlist* film on the
    title path and an inferred taste follow on the people path, and the onboarding screen
    reports those as two different numbers (`import_job.watchlist_created` and
    `follows_created`).

    Through `normalise_entity_id` for the same reason the routes are: `app.follow.entity_id` is
    polymorphic text with no foreign key, so two spellings of one id are two follow rows that
    nothing will ever reconcile.

    **A follow made this way now reaches every credit** (EF-1, EF-2): there is no tier left to
    keep an inferred follow quieter than a chosen one. That is why EF-20 deletes this path —
    and why NEU-1432's migration deletes the rows it already wrote. It survives here only
    until M5 lands."""
    _, _, created = await follow_service.follow(
        db,
        user=user,
        entity_type=entity_type,
        entity_id=normalise_entity_id(entity_type, entity_id),
        source=source,
    )
    return created


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


async def people_for(db: AsyncSession, client: TMDBClient, tmdb_id: int) -> list[int] | None:
    """The TMDB person ids a film contributes — its director(s) and top-2 billing — or `None`
    when TMDB no longer has it.

    Reads them out of the catalog when we already hold the film's credits and spends a
    `/movie/{id}` only otherwise, which on a re-run is the difference between minutes and
    seconds."""
    local = await _local_people(db, tmdb_id)
    if local is not None:
        return local

    details = await fetch_details(client, tmdb_id)
    if details is None:
        return None
    members = _promoted_members(details)
    await upsert_people(db, members)
    await db.commit()
    return list(dict.fromkeys(m.id for m in members))


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


async def _local_people(db: AsyncSession, tmdb_id: int) -> list[int] | None:
    """The people read out of `catalog.film_credit`, or `None` if we cannot answer from the
    catalog — the film is absent, or is present but has never had its credits read.

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
    """The director(s) and top-2 billed cast of a film, directors first.

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


def _is_fresh(credits_observed_at: datetime | None) -> bool:
    return (
        credits_observed_at is not None and datetime.now(UTC) - credits_observed_at <= CREDITS_FRESH
    )


async def finalize_failed(job_id: UUID, error: str) -> None:
    """Move a job to `failed` on its own session, for a runner whose own session is gone.

    Its own session factory rather than the runner's, because the crash this answers may be the
    runner's session dying — reusing it would fail the write that records the failure."""
    async with SessionLocal() as db:
        await import_job_repo.finalize(db, job_id, status="failed", error=error)
        await db.commit()
