"""Remove the `trailer` cards the video poll raised for films that had already opened (NEU-1532).

One-off data repair, not a migration: it edits rows rather than schema, it is only meaningful
against production, and Alembic running it on every environment would be wrong.

## What happened

D-35's video poll read the provider poll's whole scoped set, most of which is films 14 to 365
days past their US theatrical date, and carded every new YouTube trailer TMDB listed for them.
On 2026-10-04 that put a `trailer` card for *Spider-Man: Brand New Day* — out since 2026-07-29 —
on the feed. Once a film is out its only beats are home media and streaming, so the fix stops
the video poll at release; this removes what it already published.

## What it removes

Catalog `trailer` events created since the video poll shipped (`VIDEOS_SHIPPED`, NEU-1385's
merge — its first run baselined, so nothing earlier exists) on a film whose primary
`release_date` was before the day the card was created: released when carded, by the date half
of the in-play rule (D-1532.2). The status half cannot be reconstructed — nothing records what
TMDB's `status` said on the poll day — so a film released by status alone, with a NULL or future
primary date, is left. That is accepted.

Story-provenance trailer cards are not touched: a trade writing one up is editorial judgement
that it is news (D-1532.1).

**Delete, not supersede** (D-1532.7, D-1505.8's reasoning). A superseded card still renders on
the feed, the film page and title-follow timelines, and needs a `superseded_by` target that
does not exist. The `catalog.film_video` rows behind the cards stay: the ledger is insert-only,
and since released films are no longer polled nothing can re-card them.

## The audit

Printed before anything is deleted, because the `app.notification` rows cascade with their
events: per card, the film, its release date, when the trailer went up, when it was carded, its
subject, and the digest rows it earned (user, status, sent).

Run it in the container after the fix deploys, dry first:

    task shell
    python scripts/prune_released_trailer_cards.py            # report only
    python scripts/prune_released_trailer_cards.py --apply    # delete
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import Date, and_, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from upmovies.app.models import Notification
from upmovies.catalog.models import Film
from upmovies.db import SessionLocal
from upmovies.news.catalog_events import TRAILER_EVENT_TYPE
from upmovies.news.models import Event, EventStory, EventSummary

log = logging.getLogger("prune_released_trailer_cards")

VIDEOS_SHIPPED = datetime(2026, 9, 19, tzinfo=UTC)
"""NEU-1385's merge: no catalog trailer card is older than this."""


def _released_trailer_cards() -> ColumnElement[bool]:
    """WHERE terms (joined to `catalog.film`) selecting the trailer cards raised for a film
    already released on the (UTC) day the card was created."""
    return and_(
        Event.provenance == "catalog",
        Event.event_type == TRAILER_EVENT_TYPE,
        Event.created_at >= VIDEOS_SHIPPED,
        Film.release_date < cast(func.timezone("UTC", Event.created_at), Date),
    )


@dataclass
class Card:
    event_id: UUID
    title: str
    release_date: date
    occurred_at: datetime
    created_at: datetime
    subject_key: list[str] | None
    # By `user_id`, not email: the dry run's output may be pasted on the PR, and other users'
    # addresses do not belong there.
    notifications: list[tuple[UUID, str, datetime | None]] = field(default_factory=list)


async def _cards(session: AsyncSession) -> list[Card]:
    rows = (
        await session.execute(
            select(
                Event.id,
                Film.title,
                Film.release_date,
                Event.occurred_at,
                Event.created_at,
                Event.subject_key,
            )
            .join(Film, Film.id == Event.film_id)
            .where(_released_trailer_cards())
            .order_by(Film.title, Event.occurred_at)
        )
    ).all()
    return [Card(*row) for row in rows]


async def _audit(session: AsyncSession, cards: list[Card]) -> None:
    """Fill in each card's digest rows. Read before any delete: they cascade with the events."""
    for card in cards:
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
    deleted: dict[str, int] = field(default_factory=dict)
    """Rows deleted per table, by table name — empty on a dry run."""


async def prune(session: AsyncSession, *, apply: bool) -> Pruned:
    """Select, audit and — with `apply` — delete the cards.

    `event_summary` and `event_story` are cleared explicitly rather than trusted to cascade, as
    the NEU-1121 and NEU-1505 precedents do; `app.notification` goes by its FK cascade."""
    pruned = Pruned(cards=await _cards(session))
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
        await session.commit()
    return pruned


def _report(cards: list[Card]) -> None:
    log.info("%d trailer cards on released films", len(cards))
    for card in cards:
        log.info(
            "  %-40s released %s  trailer %s  carded %s  %s",
            card.title[:40],
            card.release_date.isoformat(),
            card.occurred_at.isoformat(),
            card.created_at.isoformat(),
            ", ".join(card.subject_key or []) or "-",
        )
        for user_id, status, sent_at in card.notifications:
            log.info(
                "      digest  %-35s %-8s %s",
                user_id,
                status,
                sent_at.isoformat() if sent_at else "-",
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
        # it reads cascade with the events.
        _report((await prune(session, apply=False)).cards)
        if args.apply:
            pruned = await prune(session, apply=True)
            for table, count in pruned.deleted.items():
                log.info("deleted %d %s rows", count, table)
        else:
            log.info("dry run — nothing deleted; re-run with --apply")


if __name__ == "__main__":
    asyncio.run(main())
