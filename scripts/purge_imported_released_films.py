"""Purge the films pre-NEU-1505 imports admitted already outside the alert window (NEU-1508).

One-off data repair, not a migration: it edits rows rather than schema, it is only meaningful
against production, and Alembic running it on every environment would be wrong.

## What happened

Before NEU-1505, an import (EF-21) upserted every film a user's watchlist named, whatever its
age, so `catalog.film` holds films that had opened years before they arrived — *The Curious
Case of Benjamin Button*, *Mank*, *The Killer* and *Beau Is Afraid* among them. NEU-1505 gated
admission and left the rows (D-1505.7); this reverses that. `prune_admission_cards.py` already
removed the cards they earned; this removes the films.

## What it removes

A film created between `IMPORTS_SHIPPED` and `--before` whose release date **at creation**
was already more than `PROVIDER_POLL_MAX_AGE_DAYS` before the (UTC) day it was created
(D-1508.1). The window, not release day, is the line: EF-21 still admits a film that opened
inside it. And the date at creation, not today's: TMDB back-dates films after we admit them,
and a film the sweep admitted undated is exactly the kind that stays. The date at creation is
the `old_value` of the film's earliest `release_date` change row when it has one — a JSON
`null` there means it arrived undated — and its current `release_date` otherwise.

Spec §4.4 ("if we announced it, we do not un-announce it") still holds: a film first observed
already outside the window was never announced.

For each film, in one transaction:

- linked stories are unlinked and marked `rejected` (D-1508.3), with the confidence cleared
  and a `link_note` as a manual unlink writes them. The FK would only null `film_id`, leaving
  `link_status = 'linked'` on a row pointing at nothing, and the linker only revisits
  `pending`;
- events go explicitly — `event_story`, `event_summary`, `event` — after the audit has read the
  `app.notification` rows that cascade with them (D-1508.6). There should be none left after
  `prune_admission_cards.py`;
- title follows are deleted, silently (D-1508.2). `app.follow` has no FK onto the film;
- the film row, and the cascade takes the rest: credits, genres, companies, release dates,
  videos, availability, change history, the resolution cache, `import_candidate`.

## The audit

Printed before anything is deleted: per film, its ref and dates; the title follows (by
`user_id` — the dry run is pasted on the PR); the linked stories; the events with their digest
rows.

Run it in the container after the fix deploys, dry first:

    task shell
    python scripts/purge_imported_released_films.py            # report only
    python scripts/purge_imported_released_films.py --apply    # delete
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import (
    Date,
    Integer,
    Text,
    case,
    cast,
    delete,
    func,
    literal,
    literal_column,
    select,
    true,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import Follow, Notification
from upmovies.catalog.models import Film, FilmFieldChange
from upmovies.catalog.ref import film_ref
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.news.models import Event, EventStory, EventSummary, Story

log = logging.getLogger("purge_imported_released_films")

IMPORTS_SHIPPED = datetime(2026, 9, 17, tzinfo=UTC)
"""NEU-1356's merge: no film older than this can have been admitted by an import."""

# NEU-1505's deploy boundary. After it, no import admits a film outside the window, so a later
# value is harmless and an earlier one misses films: if the deploy lands after this date, pass
# `--before` with the deploy's timestamp.
DEFAULT_CUTOFF = datetime(2026, 9, 28, tzinfo=UTC)

PURGED_LINK_NOTE = "purged-film"
"""The `link_note` on a story this script unlinked — `link.moderation._reject`'s shape, so a
rejected row carries no confidence and says why it was rejected."""


@dataclass
class Candidate:
    film_id: UUID
    tmdb_id: int
    title: str
    release_date_at_creation: date
    created_at: datetime
    # By `user_id`, not email: the dry run's output is pasted on the PR.
    follows: list[tuple[UUID, str]] = field(default_factory=list)
    stories: list[tuple[UUID, str]] = field(default_factory=list)
    events: list["PurgedEvent"] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return film_ref(self.tmdb_id, self.title)


@dataclass
class PurgedEvent:
    event_id: UUID
    event_type: str
    occurred_at: datetime
    notifications: list[tuple[UUID, str, datetime | None]] = field(default_factory=list)


async def _candidates(session: AsyncSession, *, before: datetime) -> list[Candidate]:
    """The films an import admitted already outside the alert window (D-1508.1).

    A film whose first `release_date` change row exists is judged by that row's `old_value`,
    even when it is JSON `null` (arrived undated → kept): falling back to the current date
    there would purge exactly the sweep films TMDB back-dated after admission."""
    first_change = (
        select(FilmFieldChange.id, FilmFieldChange.old_value)
        .where(FilmFieldChange.film_id == Film.id, FilmFieldChange.field == "release_date")
        .order_by(FilmFieldChange.changed_at, FilmFieldChange.id)
        .limit(1)
        .lateral("first_change")
    )
    at_creation = cast(
        case(
            (
                first_change.c.id.is_not(None),
                first_change.c.old_value.op("#>>", return_type=Text)(
                    literal_column("'{}'::text[]")
                ),
            ),
            else_=cast(Film.release_date, Text),
        ),
        Date,
    )
    created_day = cast(func.timezone("UTC", Film.created_at), Date)
    max_age_days = cast(literal(get_settings().provider_poll_max_age_days), Integer)
    rows = (
        await session.execute(
            select(Film.id, Film.tmdb_id, Film.title, at_creation, Film.created_at)
            .outerjoin(first_change, true())
            .where(
                Film.created_at >= IMPORTS_SHIPPED,
                Film.created_at < before,
                at_creation.is_not(None),
                at_creation < created_day - max_age_days,
            )
            .order_by(Film.title, Film.tmdb_id)
        )
    ).all()
    return [Candidate(*row) for row in rows]


async def _audit(session: AsyncSession, candidates: list[Candidate]) -> None:
    """Fill in each film's follows, linked stories and events. Read before any delete: the
    notification rows cascade with the events, and nothing else logs who followed the film."""
    for film in candidates:
        film.follows = [
            (user_id, source)
            for user_id, source in (
                await session.execute(
                    select(Follow.user_id, Follow.source)
                    .where(Follow.entity_type == "title", Follow.entity_id == str(film.film_id))
                    .order_by(Follow.user_id)
                )
            ).all()
        ]
        film.stories = [
            (story_id, title)
            for story_id, title in (
                await session.execute(
                    select(Story.id, Story.title)
                    .where(Story.film_id == film.film_id)
                    .order_by(Story.published_at, Story.id)
                )
            ).all()
        ]
        film.events = [
            PurgedEvent(*row)
            for row in (
                await session.execute(
                    select(Event.id, Event.event_type, Event.occurred_at)
                    .where(Event.film_id == film.film_id)
                    .order_by(Event.occurred_at, Event.id)
                )
            ).all()
        ]
        for event in film.events:
            event.notifications = [
                (user_id, status, sent_at)
                for user_id, status, sent_at in (
                    await session.execute(
                        select(Notification.user_id, Notification.status, Notification.sent_at)
                        .where(Notification.event_id == event.event_id)
                        .order_by(Notification.user_id)
                    )
                ).all()
            ]


@dataclass
class Purged:
    films: list[Candidate]


async def purge(session: AsyncSession, *, apply: bool, before: datetime = DEFAULT_CUTOFF) -> Purged:
    """Select, audit and — with `apply` — remove the films and what hangs off them.

    Stories are unlinked before the film goes so no row claims a link it does not have; events
    are deleted explicitly, `event_summary` and `event_story` first, as the NEU-1121 and
    NEU-1505 precedents do; the title follows go by hand because nothing cascades into
    `app.follow`. Everything else is the film's own and leaves with it."""
    films = await _candidates(session, before=before)
    await _audit(session, films)
    if apply and films:
        film_ids = [f.film_id for f in films]
        event_ids = [e.event_id for f in films for e in f.events]
        await session.execute(
            update(Story)
            .where(Story.film_id.in_(film_ids))
            .values(
                film_id=None,
                link_status="rejected",
                link_confidence=None,
                link_note=PURGED_LINK_NOTE,
                linked_at=func.now(),
            )
        )
        if event_ids:
            await session.execute(delete(EventStory).where(EventStory.event_id.in_(event_ids)))
            await session.execute(delete(EventSummary).where(EventSummary.event_id.in_(event_ids)))
            await session.execute(delete(Event).where(Event.id.in_(event_ids)))
        await session.execute(
            delete(Follow).where(
                Follow.entity_type == "title", Follow.entity_id.in_([str(i) for i in film_ids])
            )
        )
        await session.execute(delete(Film).where(Film.id.in_(film_ids)))
        await session.commit()
    return Purged(films=films)


def _utc(value: str) -> datetime:
    """`--before`, read as UTC when it carries no offset: it is compared to `timestamptz`."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _report(purged: Purged) -> None:
    log.info("%d films admitted already outside the alert window", len(purged.films))
    for film in purged.films:
        log.info(
            "  %-40s %-8d %s  released %s  created %s",
            film.title[:40],
            film.tmdb_id,
            film.ref,
            film.release_date_at_creation.isoformat(),
            film.created_at.isoformat(),
        )
        for user_id, source in film.follows:
            log.info("      follows %-36s %s", user_id, source)
        for story_id, title in film.stories:
            log.info("      story   %s  %s", story_id, title[:60])
        for event in film.events:
            log.info("      event   %-20s %s", event.event_type, event.occurred_at.isoformat())
            for user_id, status, sent_at in event.notifications:
                log.info(
                    "          digest  %-35s %-8s %s",
                    user_id,
                    status,
                    sent_at.isoformat() if sent_at else "-",
                )
    log.info(
        "totals: %d films, %d follows, %d stories, %d events",
        len(purged.films),
        sum(len(f.follows) for f in purged.films),
        sum(len(f.stories) for f in purged.films),
        sum(len(f.events) for f in purged.films),
    )


async def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="actually delete (default is report only)"
    )
    parser.add_argument(
        "--before",
        type=_utc,
        default=DEFAULT_CUTOFF,
        help=f"only films created before this ISO timestamp (default {DEFAULT_CUTOFF.date()})",
    )
    args = parser.parse_args(argv)

    async with SessionLocal() as session:
        # The audit is printed from a dry pass first, whatever the flag: the notification rows
        # it reads cascade with the events.
        _report(await purge(session, apply=False, before=args.before))
        if args.apply:
            purged = await purge(session, apply=True, before=args.before)
            log.info("deleted %d films and the rows above", len(purged.films))
        else:
            log.info("dry run — nothing deleted; re-run with --apply")


if __name__ == "__main__":
    asyncio.run(main())
