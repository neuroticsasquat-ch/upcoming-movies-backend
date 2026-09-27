"""Remove the attachment cards admission wrote for films that were already released (NEU-1505).

One-off data repair, not a migration: it edits rows rather than schema, it is only meaningful
against production, and Alembic running it on every environment would be wrong.

## What happened

EF-4's admission exception (NEU-1436) wrote every followed credit, studio and franchise on a
film's *first observation* as an attachment, with no release check. On the night of 2026-09-26
another user's Letterboxd import first-observed four long-released David Fincher films, and the
2026-09-27 digest carried `crew_attached` cards for *Mank*, *The Curious Case of Benjamin
Button*, *The Killer* and *Beau Is Afraid*. The fix gates admission on `is_unreleased`; this
removes what the ungated rule already published.

## What it removes

Catalog cards of the four attachment types whose change rows were written by an admission of
a film that had already opened. An admission is recognised by its timestamp: Postgres `now()`
is the transaction's start time, so an admission's rows and the marker it sets share one value
exactly —

- `casting` / `crew_attached`: `occurred_at = film.credits_observed_at`;
- `company_attached`: `occurred_at = film.companies_observed_at`;
- `collection_attached`: `occurred_at = film.created_at`. The synthetic `collection_id` row is
  written in the transaction that inserts the film (`record_collection_admission`), and the
  `BEFORE UPDATE` trigger cannot write one then, so this equality names exactly that row.

"Already opened" is the film's primary `release_date` before the day of that marker — the
admission's own date, rather than a fixed cutoff, so a film admitted unreleased that has opened
since keeps its genuine card. The marker must also fall between NEU-1436's deploy
(`ADMISSION_SHIPPED`) and this fix's (`DEFAULT_CUTOFF`): nothing before the first wrote
admission rows, and nothing after the second writes them for a released film.

The **change rows** behind those cards go too, selected by the same markers — and so do the
ones not carded yet. The sweep's credit phase looks back seven days, so a row left behind is
carded again on the next run, and a row still inside its 72-hour quarantine would card for the
first time.

**Delete, not supersede** (D-1505.8). A superseded card still renders on the public feed, the
film page and title-follow timelines (only the entity arms and the digest exclude it), and needs
a removal event for `superseded_by` to point at, which does not exist. Precedent for deleting
cards the film page contradicts: `prune_primary_release_events.py` (NEU-1121).

## The audit

Printed before anything is deleted, because the `app.notification` rows cascade with their
events: per card, the film, type, subject and date; the digest rows it earned (user, status,
sent); and the users whose follows put it on their timeline, which is logged nowhere else.

Run it in the container after the fix deploys, dry first:

    task shell
    python scripts/prune_admission_cards.py            # report only
    python scripts/prune_admission_cards.py --apply    # delete
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Date, and_, cast, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from upmovies.app.models import Follow, Notification
from upmovies.catalog.models import (
    COLLECTION_FIELD,
    Film,
    FilmCompanyChange,
    FilmCreditChange,
    FilmFieldChange,
)
from upmovies.db import SessionLocal
from upmovies.news.catalog_events import (
    COLLECTION_ATTACHED_EVENT_TYPE,
    COMPANY_ATTACHED_EVENT_TYPE,
)
from upmovies.news.models import Event, EventStory, EventSummary

log = logging.getLogger("prune_admission_cards")

ADMISSION_SHIPPED = datetime(2026, 9, 22, tzinfo=UTC)
"""NEU-1436's deploy: no admission row is older than this."""

# The fix's deploy boundary. After it, admission writes nothing for a released film, so a later
# value is harmless and an earlier one misses rows: if the deploy lands after this date, pass
# `--before` with the deploy's timestamp.
DEFAULT_CUTOFF = datetime(2026, 9, 28, tzinfo=UTC)

CREDIT_EVENT_TYPES = ("casting", "crew_attached")


def _released_before(marker: ColumnElement) -> ColumnElement[bool]:
    """The film had opened by the (UTC) day of `marker` — `is_unreleased`'s date half, negated,
    asked of the admission's own date."""
    return Film.release_date < cast(func.timezone("UTC", marker), Date)


def _admission(marker: ColumnElement, *, before: datetime) -> ColumnElement[bool]:
    return and_(marker >= ADMISSION_SHIPPED, marker < before, _released_before(marker))


def _admission_cards(*, before: datetime) -> ColumnElement[bool]:
    """WHERE terms (joined to `catalog.film`) selecting the cards a released admission wrote."""
    return and_(
        Event.provenance == "catalog",
        or_(
            and_(
                Event.event_type.in_(CREDIT_EVENT_TYPES),
                Event.occurred_at == Film.credits_observed_at,
                _admission(Film.credits_observed_at, before=before),
            ),
            and_(
                Event.event_type == COMPANY_ATTACHED_EVENT_TYPE,
                Event.occurred_at == Film.companies_observed_at,
                _admission(Film.companies_observed_at, before=before),
            ),
            and_(
                Event.event_type == COLLECTION_ATTACHED_EVENT_TYPE,
                Event.occurred_at == Film.created_at,
                _admission(Film.created_at, before=before),
            ),
        ),
    )


@dataclass
class Card:
    event_id: UUID
    film_id: UUID
    title: str
    event_type: str
    subject_key: list[str] | None
    occurred_at: datetime
    # By `user_id`, not email: the dry run's output is pasted on the PR (§3.4), and other users'
    # addresses do not belong there.
    notifications: list[tuple[UUID, str, datetime | None]] = field(default_factory=list)
    followers: set[UUID] = field(default_factory=set)


async def _cards(session: AsyncSession, *, before: datetime) -> list[Card]:
    rows = (
        await session.execute(
            select(
                Event.id,
                Event.film_id,
                Film.title,
                Event.event_type,
                Event.subject_key,
                Event.occurred_at,
            )
            .join(Film, Film.id == Event.film_id)
            .where(_admission_cards(before=before))
            .order_by(Film.title, Event.event_type)
        )
    ).all()
    return [Card(*row) for row in rows]


async def _audit(session: AsyncSession, cards: list[Card]) -> None:
    """Fill in each card's digest rows and timeline reach. Read before any delete: the
    notification rows cascade with the events."""
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
        card.followers = set(
            (
                await session.execute(
                    select(Follow.user_id).where(or_(*await _subjects(session, card)))
                )
            )
            .scalars()
            .all()
        )


async def _subjects(session: AsyncSession, card: Card) -> list[ColumnElement[bool]]:
    """The follows that put this card on a timeline: the film's own title follows, and the
    entities its admission rows named — for a credit card, only the rows of its own kind: a
    `casting` card names the cast admitted with it, a `crew_attached` card the crew."""
    film = await session.get(Film, card.film_id)
    assert film is not None
    terms = [and_(Follow.entity_type == "title", Follow.entity_id == str(card.film_id))]
    if card.event_type in CREDIT_EVENT_TYPES:
        people = (
            await session.execute(
                select(FilmCreditChange.person_id).where(
                    FilmCreditChange.film_id == card.film_id,
                    FilmCreditChange.changed_at == card.occurred_at,
                    FilmCreditChange.credit_type
                    == ("cast" if card.event_type == "casting" else "crew"),
                )
            )
        ).scalars()
        ids = [str(p) for p in people]
        terms.append(and_(Follow.entity_type == "person", Follow.entity_id.in_(ids)))
    elif card.event_type == COMPANY_ATTACHED_EVENT_TYPE:
        companies = (
            await session.execute(
                select(FilmCompanyChange.company_id).where(
                    FilmCompanyChange.film_id == card.film_id,
                    FilmCompanyChange.changed_at == card.occurred_at,
                )
            )
        ).scalars()
        ids = [str(c) for c in companies]
        terms.append(and_(Follow.entity_type == "company", Follow.entity_id.in_(ids)))
    elif film.collection_id is not None:
        terms.append(
            and_(Follow.entity_type == "franchise", Follow.entity_id == str(film.collection_id))
        )
    return terms


async def _change_row_ids(session: AsyncSession, *, before: datetime) -> dict[type, list[int]]:
    """Every admission change row on a film that had already opened, carded or not."""
    credit = (
        select(FilmCreditChange.id)
        .join(Film, Film.id == FilmCreditChange.film_id)
        .where(
            FilmCreditChange.changed_at == Film.credits_observed_at,
            _admission(Film.credits_observed_at, before=before),
        )
    )
    company = (
        select(FilmCompanyChange.id)
        .join(Film, Film.id == FilmCompanyChange.film_id)
        .where(
            FilmCompanyChange.changed_at == Film.companies_observed_at,
            _admission(Film.companies_observed_at, before=before),
        )
    )
    collection = (
        select(FilmFieldChange.id)
        .join(Film, Film.id == FilmFieldChange.film_id)
        .where(
            FilmFieldChange.field == COLLECTION_FIELD,
            FilmFieldChange.changed_at == Film.created_at,
            _admission(Film.created_at, before=before),
        )
    )
    return {
        model: list((await session.execute(stmt)).scalars().all())
        for model, stmt in (
            (FilmCreditChange, credit),
            (FilmCompanyChange, company),
            (FilmFieldChange, collection),
        )
    }


@dataclass
class Pruned:
    cards: list[Card]
    change_rows: dict[type, list[int]]


async def prune(session: AsyncSession, *, apply: bool, before: datetime = DEFAULT_CUTOFF) -> Pruned:
    """Select, audit and — with `apply` — delete the cards and their change rows.

    `event_summary` and `event_story` are cleared explicitly rather than trusted to cascade, as
    the NEU-1121 precedent does. The events go before the change rows: a credit change row's
    `carded_by_event_id` points at its card, and is NULL on the sweep-carded rows anyway."""
    cards = await _cards(session, before=before)
    await _audit(session, cards)
    change_rows = await _change_row_ids(session, before=before)
    if apply:
        ids = [c.event_id for c in cards]
        if ids:
            await session.execute(delete(EventStory).where(EventStory.event_id.in_(ids)))
            await session.execute(delete(EventSummary).where(EventSummary.event_id.in_(ids)))
            await session.execute(delete(Event).where(Event.id.in_(ids)))
        for model, row_ids in change_rows.items():
            if row_ids:
                await session.execute(delete(model).where(model.id.in_(row_ids)))
        await session.commit()
    return Pruned(cards=cards, change_rows=change_rows)


def _utc(value: str) -> datetime:
    """`--before`, read as UTC when it carries no offset: it is compared to `timestamptz`."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _report(pruned: Pruned) -> None:
    log.info("%d admission cards on released films", len(pruned.cards))
    for card in pruned.cards:
        log.info(
            "  %-40s %-20s %s  %s",
            card.title[:40],
            card.event_type,
            card.occurred_at.isoformat(),
            ", ".join(card.subject_key or []) or "-",
        )
        for user_id, status, sent_at in card.notifications:
            log.info(
                "      digest  %-35s %-8s %s",
                user_id,
                status,
                sent_at.isoformat() if sent_at else "-",
            )
        for user_id in sorted(card.followers):
            log.info("      follows %s", user_id)
    for model, row_ids in pruned.change_rows.items():
        log.info("%d %s rows", len(row_ids), model.__tablename__)


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
        help=f"only admissions before this ISO timestamp (default {DEFAULT_CUTOFF.date()})",
    )
    args = parser.parse_args(argv)

    async with SessionLocal() as session:
        # The audit is printed from a dry pass first, whatever the flag: the notification rows
        # it reads cascade with the events.
        _report(await prune(session, apply=False, before=args.before))
        if args.apply:
            pruned = await prune(session, apply=True, before=args.before)
            log.info("deleted %d events and the change rows above", len(pruned.cards))
        else:
            log.info("dry run — nothing deleted; re-run with --apply")


if __name__ == "__main__":
    asyncio.run(main())
