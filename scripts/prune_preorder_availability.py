"""Remove the `now_available` cards pre-orders raised, with the ledger rows behind them (NEU-1538).

One-off data repair, not a migration: it edits rows rather than schema, it is only meaningful
against production, and Alembic running it on every environment would be wrong.

## What happened

Fandango At Home lists films for purchase weeks before they can be watched, and TMDB passes the
listing on as an ordinary `buy` offer with no pre-order flag. The provider poll believed it, so
a film still in cinemas carded `now_available` and the feed, film page, title-follow timelines
and digest said it was out at home. The fix (D-1538.1) believes an offer only once the film's
US digital date has landed; this removes what the poll already published.

## What it removes

Catalog `now_available` events whose film fails that same gate —
`catalog.queries.home_release_landed_clause`, one definition shared with the poll (D-1538.4) —
as of the UTC day of the card's `occurred_at`, which is the day the poll observed the offers:
the film's US digital governing date was missing, or after that day. The date is the one the
catalog holds *now*, so a film TMDB has dated since is judged against its real date. Films TMDB
has never dated are selected whether the card was a pre-order or not; that is D-1538.1's
accepted cost applied retroactively, and the audit shows them.

**Delete, not supersede** (D-1532.7's reasoning): a superseded card still renders, and needs a
`superseded_by` target that does not exist.

**The ledger rows go too** (D-1538.5), unlike the trailer prune. `availability_first_seen` is
insert-only and `now_available` cards a type the film has no row under, so a pre-order row
under `buy` would keep the film silent for the real release forever. That is why the rows taken
are not only the ones the card's observation inserted (same film and region, `first_seen_at ==
occurred_at`, the join `rerender_now_available_summaries` reads them back by) but **every row
on a pruned card's film that fails the same gate as of its own UTC `first_seen_at` day**. A
second store listing the pre-order a day later inserted a `buy` row and carded nothing, since
`buy` was already known; matching on the card's stamp alone would leave that row, and the real
release would still find `buy` known. Rows observed on or after the date stay. After this
runs, the next poll re-judges each film: still undated is held, a date that has since passed
cards again.

## The audit

Printed before anything is deleted, because the `app.notification` rows cascade with their
events: per card, the film, its tmdb id, the US digital date the catalog holds now, when it was
carded, its subject, the providers on its observation's ledger rows, and the digest rows it
earned (user, status, sent); then every ledger row about to go.

Run it in the container **after the gate deploys** — run first, the next poll would re-card the
same pre-orders — dry first:

    task shell
    python scripts/prune_preorder_availability.py            # report only
    python scripts/prune_preorder_availability.py --apply    # delete
"""

import argparse
import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Date, and_, cast, delete, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import ScalarSelect

from upmovies.app.models import Notification
from upmovies.catalog.models import AvailabilityFirstSeen, Film, FilmReleaseDate, WatchProvider
from upmovies.catalog.queries import home_release_landed_clause
from upmovies.catalog.release_grade import HOME_RELEASE_TYPES, PRIMARY_REGION
from upmovies.db import SessionLocal
from upmovies.news.catalog_events import NOW_AVAILABLE_EVENT_TYPE
from upmovies.news.models import Event, EventStory, EventSummary

log = logging.getLogger("prune_preorder_availability")


def _preorder_cards() -> ColumnElement[bool]:
    """WHERE terms (joined to `catalog.film`) selecting the `now_available` cards raised before
    the film's US digital date, as of the (UTC) day the poll observed the offers."""
    observed_on = cast(func.timezone("UTC", Event.occurred_at), Date)
    return and_(
        Event.provenance == "catalog",
        Event.event_type == NOW_AVAILABLE_EVENT_TYPE,
        not_(home_release_landed_clause(as_of=observed_on)),
    )


def _us_digital_date() -> ScalarSelect[date]:
    """The film's US digital governing date as the catalog holds it now, as the UTC day the
    gate judges — NULL when it has none. For the audit."""
    return (
        select(func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)))
        .where(
            FilmReleaseDate.film_id == Film.id,
            FilmReleaseDate.iso_3166_1 == PRIMARY_REGION,
            FilmReleaseDate.release_type.in_(tuple(sorted(HOME_RELEASE_TYPES))),
        )
        .scalar_subquery()
    )


@dataclass
class Card:
    event_id: UUID
    film_id: UUID
    region: str | None
    title: str
    tmdb_id: int
    us_digital: date | None
    occurred_at: datetime
    subject_key: list[str] | None
    providers: list[tuple[str, str]] = field(default_factory=list)
    """(monetization type, provider name) for each ledger row the card's observation inserted."""
    # By `user_id`, not email: the dry run's output may be pasted on the PR, and other users'
    # addresses do not belong there.
    notifications: list[tuple[UUID, str, datetime | None]] = field(default_factory=list)


async def _cards(session: AsyncSession) -> list[Card]:
    rows = (
        await session.execute(
            select(
                Event.id,
                Event.film_id,
                Event.region,
                Film.title,
                Film.tmdb_id,
                _us_digital_date(),
                Event.occurred_at,
                Event.subject_key,
            )
            .join(Film, Film.id == Event.film_id)
            .where(_preorder_cards())
            .order_by(Film.title, Event.occurred_at)
        )
    ).all()
    return [Card(*row) for row in rows]


def _observation_rows(card: Card) -> ColumnElement[bool]:
    """The `availability_first_seen` rows `card`'s observation inserted."""
    return and_(
        AvailabilityFirstSeen.film_id == card.film_id,
        AvailabilityFirstSeen.region == card.region,
        AvailabilityFirstSeen.first_seen_at == card.occurred_at,
    )


def _preorder_ledger_rows(film_ids: Collection[UUID]) -> ColumnElement[bool]:
    """WHERE terms (joined to `catalog.film`) selecting the ledger rows on these films that the
    gate would have held: observed before the film's US digital date, as of the row's UTC day.

    A superset of each card's observation rows — every one of those failed the gate the same
    day its card did — that also takes the rows a later pre-order sighting inserted uncarded."""
    seen_on = cast(func.timezone("UTC", AvailabilityFirstSeen.first_seen_at), Date)
    return and_(
        AvailabilityFirstSeen.film_id.in_(film_ids),
        not_(home_release_landed_clause(as_of=seen_on)),
    )


@dataclass
class LedgerRow:
    id: int
    title: str
    tmdb_id: int
    monetization_type: str
    provider: str
    first_seen_at: datetime


async def _ledger(session: AsyncSession, cards: list[Card]) -> list[LedgerRow]:
    if not cards:
        return []
    rows = (
        await session.execute(
            select(
                AvailabilityFirstSeen.id,
                Film.title,
                Film.tmdb_id,
                AvailabilityFirstSeen.monetization_type,
                WatchProvider.name,
                AvailabilityFirstSeen.first_seen_at,
            )
            .join(Film, Film.id == AvailabilityFirstSeen.film_id)
            .join(WatchProvider, WatchProvider.id == AvailabilityFirstSeen.provider_id)
            .where(_preorder_ledger_rows({c.film_id for c in cards}))
            .order_by(Film.title, AvailabilityFirstSeen.id)
        )
    ).all()
    return [LedgerRow(*row) for row in rows]


async def _audit(session: AsyncSession, cards: list[Card]) -> None:
    """Fill in each card's ledger providers and digest rows. Read before any delete: the
    notifications cascade with the events, and the ledger rows go with them."""
    for card in cards:
        card.providers = [
            (kind, name)
            for kind, name in (
                await session.execute(
                    select(AvailabilityFirstSeen.monetization_type, WatchProvider.name)
                    .join(WatchProvider, WatchProvider.id == AvailabilityFirstSeen.provider_id)
                    .where(_observation_rows(card))
                    .order_by(AvailabilityFirstSeen.id)
                )
            ).all()
        ]
        card.notifications = [
            (user_id, status, sent_at)
            for user_id, status, sent_at in (
                await session.execute(
                    select(Notification.user_id, Notification.status, Notification.sent_at)
                    .where(Notification.event_id == card.event_id)
                    .order_by(Notification.user_id)
                )
            ).all()
        ]


@dataclass
class Pruned:
    cards: list[Card]
    ledger: list[LedgerRow] = field(default_factory=list)
    """Every `availability_first_seen` row that goes with the cards."""
    deleted: dict[str, int] = field(default_factory=dict)
    """Rows deleted per table, by table name — empty on a dry run."""


async def prune(session: AsyncSession, *, apply: bool) -> Pruned:
    """Select, audit and — with `apply` — delete the cards and their ledger rows.

    `event_summary` and `event_story` are cleared explicitly rather than trusted to cascade, as
    the NEU-1121, NEU-1505 and NEU-1532 precedents do; `app.notification` goes by its FK
    cascade."""
    cards = await _cards(session)
    pruned = Pruned(cards=cards, ledger=await _ledger(session, cards))
    await _audit(session, pruned.cards)
    if apply:
        ids = [c.event_id for c in pruned.cards]
        if ids:
            for model, column in (
                (EventStory, EventStory.event_id),
                (EventSummary, EventSummary.event_id),
                (Event, Event.id),
            ):
                result = await session.execute(delete(model).where(column.in_(ids)))
                # CursorResult has rowcount
                pruned.deleted[model.__tablename__] = result.rowcount or 0  # type: ignore[attr-defined]
            result = await session.execute(
                delete(AvailabilityFirstSeen).where(
                    AvailabilityFirstSeen.id.in_([row.id for row in pruned.ledger])
                )
            )
            pruned.deleted[AvailabilityFirstSeen.__tablename__] = result.rowcount or 0  # type: ignore[attr-defined]
        await session.commit()
    return pruned


def _report(pruned: Pruned) -> None:
    cards = pruned.cards
    log.info("%d now_available cards raised before the film's US digital date", len(cards))
    for card in cards:
        log.info(
            "  %-40s tmdb %-8d digital %-10s  carded %s  %s",
            card.title[:40],
            card.tmdb_id,
            card.us_digital.isoformat() if card.us_digital is not None else "none",
            card.occurred_at.isoformat(),
            ", ".join(card.subject_key or []) or "-",
        )
        for kind, name in card.providers:
            log.info("      ledger  %-8s %s", kind, name)
        for user_id, status, sent_at in card.notifications:
            log.info(
                "      digest  %-35s %-8s %s",
                user_id,
                status,
                sent_at.isoformat() if sent_at else "-",
            )
    log.info("%d ledger rows observed before the film's US digital date", len(pruned.ledger))
    for row in pruned.ledger:
        log.info(
            "  %-40s tmdb %-8d %-8s %-25s first seen %s",
            row.title[:40],
            row.tmdb_id,
            row.monetization_type,
            row.provider[:25],
            row.first_seen_at.isoformat(),
        )


async def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="actually delete (default is report only)"
    )
    args = parser.parse_args(argv)

    async with SessionLocal() as session:
        # The audit is printed from a dry pass first, whatever the flag: the notification rows
        # it reads cascade with the events, and the ledger rows it names are deleted with them.
        _report(await prune(session, apply=False))
        if args.apply:
            pruned = await prune(session, apply=True)
            for table, count in pruned.deleted.items():
                log.info("deleted %d %s rows", count, table)
        else:
            log.info("dry run — nothing deleted; re-run with --apply")


if __name__ == "__main__":
    asyncio.run(main())
