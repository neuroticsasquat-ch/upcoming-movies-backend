"""Remove the US physical release date from the `release_date` cards already raised (NEU-1542).

One-off data repair, not a migration: it edits rows rather than schema, it is only meaningful
against production, and Alembic running it on every environment would be wrong.

## What happened

D-26 put the US home release on the product as two buckets, digital (TMDB type 4) and physical
(type 5), so the sweep carded a disc date like any other release date: a `release_date` event
with a `US:physical` subject token and a "US physical release date set to …" body. NEU-1542
(ADR-0023) took physical out of the displayable set — the site follows a film to the first day
you can watch it at home, and the disc almost always lands after that. Narrowing the set cards
nothing new and stops the sweep carding physical moves; this repairs what it already published.

## What it changes

Catalog `release_date` events whose `subject_key` carries `US:physical`. Story-provenance cards
have no subject token and are never touched. Two shapes exist, because a theatrical and a home
move in one observation share one card (`sweep.release_events`):

- **Physical-only** (every token is `US:physical`): the event is **deleted**, with its summary,
  its story links and — by FK cascade — its digest rows. Delete, not supersede, for the reason
  D-1532.7 gives: a superseded card still renders, and there is no target to supersede it with.
- **Mixed**: the event loses its `US:physical` token, and its body is **re-rendered** from the
  `film_release_date_change` rows behind the surviving tokens — the same observation, so the
  same `changed_at` the card's `occurred_at` records — through the same `render_change` →
  `ReleaseDatesChanged` → `render_summary` path the sweep writes with. A body edited by hand,
  or not written by the deterministic writer, is left as it is and counted; so is one whose
  change rows cannot all be found, rather than rendering a card that names fewer moves than it
  is about. Either way the token goes, and `Event.region` is re-derived from the surviving
  tokens the way the sweep derives it (`US` when present, else the first region), so a card
  left holding only an origin-country move is tagged with that market.

The `film_release_date_change` rows of type 5 stay: they are history, and the sweep's loader no
longer reads a non-displayable type, so nothing can re-card them.

## The audit

Printed before anything is written, because the `app.notification` rows cascade with deleted
events: per card, the film, when the observation was, the tokens before and after, the body
before and after (or "deleted"), and the digest rows it earned (user, status, sent). Then the
totals per outcome.

Run it in the container after NEU-1542 deploys, dry first:

    task shell
    python scripts/prune_physical_release_cards.py            # report only
    python scripts/prune_physical_release_cards.py --apply    # write
"""

import argparse
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import Notification
from upmovies.catalog.models import Film, FilmReleaseDateChange
from upmovies.catalog.release_grade import RELEASE_TYPE_BUCKETS, release_bucket
from upmovies.db import SessionLocal
from upmovies.ingest.sweep.release_events import EVENT_TYPE, ReleaseDateMove, render_change
from upmovies.news.models import Event, EventStory, EventSummary
from upmovies.synthesize.deterministic import (
    DETERMINISTIC_MODEL,
    TEMPLATE_VERSION,
    ReleaseDatesChanged,
    render_summary,
)

log = logging.getLogger("prune_physical_release_cards")

PHYSICAL_TOKEN = "US:physical"
"""The subject token D-26's physical bucket carded under. Home release is US-only, so there is
no other region's spelling to look for."""

DELETED = "deleted"
RE_RENDERED = "re-rendered"
KEPT_EDITED = "token only (hand-edited or non-deterministic body)"
KEPT_NO_CHANGES = "token only (no change rows)"


@dataclass
class Card:
    event_id: UUID
    film_id: UUID
    title: str
    occurred_at: datetime
    tokens_before: list[str]
    body_before: str | None
    tokens_after: list[str] = field(default_factory=list)
    body_after: str | None = None
    """The re-rendered body; `None` when the card is deleted or keeps its body."""
    outcome: str = DELETED
    # By `user_id`, not email: the dry run's output may be pasted on the PR, and other users'
    # addresses do not belong there.
    notifications: list[tuple[UUID, str, datetime | None]] = field(default_factory=list)


async def _surviving_moves(
    session: AsyncSession, *, film_id: UUID, changed_at: datetime, tokens: list[str]
) -> list[ReleaseDateMove] | None:
    """The change rows behind `tokens` in this observation, in the order the sweep read them,
    or `None` if any token has no row to rebuild from."""
    rows = (
        await session.execute(
            select(
                FilmReleaseDateChange.film_id,
                FilmReleaseDateChange.iso_3166_1,
                FilmReleaseDateChange.release_type,
                FilmReleaseDateChange.previous_date,
                FilmReleaseDateChange.new_date,
                FilmReleaseDateChange.change,
                FilmReleaseDateChange.changed_at,
            )
            .where(
                FilmReleaseDateChange.film_id == film_id,
                FilmReleaseDateChange.changed_at == changed_at,
                FilmReleaseDateChange.release_type.in_(tuple(RELEASE_TYPE_BUCKETS)),
            )
            .order_by(FilmReleaseDateChange.id)
        )
    ).all()
    moves = [
        ReleaseDateMove(*row)
        for row in rows
        if f"{row.iso_3166_1}:{release_bucket(row.release_type)}" in tokens
    ]
    found = {f"{m.iso_3166_1}:{release_bucket(m.release_type)}" for m in moves}
    return moves if found == set(tokens) else None


async def _plan(session: AsyncSession) -> list[Card]:
    """Every catalog `release_date` card carrying the physical token, with what will happen to
    it. Reads only."""
    rows = (
        await session.execute(
            select(
                Event.id,
                Event.film_id,
                Film.title,
                Event.occurred_at,
                Event.subject_key,
                EventSummary.summary,
                EventSummary.model,
                EventSummary.edited_at,
            )
            .join(Film, Film.id == Event.film_id)
            .outerjoin(EventSummary, EventSummary.event_id == Event.id)
            .where(
                Event.provenance == "catalog",
                Event.event_type == EVENT_TYPE,
                Event.subject_key.overlap([PHYSICAL_TOKEN]),
            )
            .order_by(Film.title, Event.occurred_at)
        )
    ).all()

    cards: list[Card] = []
    for event_id, film_id, title, occurred_at, tokens, body, model, edited_at in rows:
        card = Card(event_id, film_id, title, occurred_at, list(tokens or []), body)
        card.tokens_after = [t for t in card.tokens_before if t != PHYSICAL_TOKEN]
        if not card.tokens_after:
            card.outcome = DELETED
        elif body is None or model != DETERMINISTIC_MODEL or edited_at is not None:
            # A body someone edited by hand is theirs, not the template's.
            card.outcome = KEPT_EDITED
        else:
            moves = await _surviving_moves(
                session, film_id=film_id, changed_at=occurred_at, tokens=card.tokens_after
            )
            if moves is None:
                card.outcome = KEPT_NO_CHANGES
            else:
                card.outcome = RE_RENDERED
                card.body_after = render_summary(
                    ReleaseDatesChanged(changes=tuple(render_change(m) for m in moves))
                )
        cards.append(card)
    return cards


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


def _region(tokens: list[str]) -> str:
    """`ReleaseDateGroup.region` over subject tokens: `US` when present, else the first region
    in sort order — so the repaired card is tagged as the sweep would have tagged it."""
    regions = {t.split(":", 1)[0] for t in tokens}
    return "US" if "US" in regions else sorted(regions)[0]


@dataclass
class Pruned:
    cards: list[Card]
    deleted: dict[str, int] = field(default_factory=dict)
    """Rows deleted per table, by table name — empty on a dry run."""
    updated: int = 0
    """Mixed cards whose token (and maybe body) was rewritten — 0 on a dry run."""

    @property
    def totals(self) -> Counter[str]:
        return Counter(card.outcome for card in self.cards)


async def prune(session: AsyncSession, *, apply: bool) -> Pruned:
    """Plan, audit and — with `apply` — write the repair, in one transaction.

    `event_summary` and `event_story` are cleared explicitly rather than trusted to cascade, as
    the trailer prune does; `app.notification` goes by its FK cascade."""
    pruned = Pruned(cards=await _plan(session))
    await _audit(session, pruned.cards)
    if not apply:
        return pruned

    doomed = [c.event_id for c in pruned.cards if c.outcome == DELETED]
    if doomed:
        for model, column in (
            (EventStory, EventStory.event_id),
            (EventSummary, EventSummary.event_id),
            (Event, Event.id),
        ):
            result = await session.execute(delete(model).where(column.in_(doomed)))
            # CursorResult has rowcount
            pruned.deleted[model.__tablename__] = result.rowcount or 0  # type: ignore[attr-defined]
    for card in pruned.cards:
        if card.outcome == DELETED:
            continue
        await session.execute(
            update(Event)
            .where(Event.id == card.event_id)
            .values(subject_key=card.tokens_after, region=_region(card.tokens_after))
        )
        if card.body_after is not None:
            await session.execute(
                update(EventSummary)
                .where(EventSummary.event_id == card.event_id)
                .values(summary=card.body_after, prompt_version=TEMPLATE_VERSION)
            )
        pruned.updated += 1
    await session.commit()
    return pruned


def _report(pruned: Pruned) -> None:
    log.info("%d release_date cards carry %s", len(pruned.cards), PHYSICAL_TOKEN)
    for card in pruned.cards:
        log.info(
            "  %-40s observed %s  %s",
            card.title[:40],
            card.occurred_at.isoformat(),
            card.outcome,
        )
        log.info(
            "      tokens  %s -> %s",
            ", ".join(card.tokens_before),
            ", ".join(card.tokens_after) or "-",
        )
        log.info("      body    %r", card.body_before)
        if card.outcome == DELETED:
            log.info("           -> deleted")
        elif card.body_after is not None:
            log.info("           -> %r", card.body_after)
        for user_id, status, sent_at in card.notifications:
            log.info(
                "      digest  %-35s %-8s %s",
                user_id,
                status,
                sent_at.isoformat() if sent_at else "-",
            )
    totals = pruned.totals
    log.info(
        "totals: %s",
        ", ".join(
            f"{outcome} {totals[outcome]}"
            for outcome in (DELETED, RE_RENDERED, KEPT_EDITED, KEPT_NO_CHANGES)
        ),
    )


async def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="actually write (default is report only)"
    )
    args = parser.parse_args(argv)

    async with SessionLocal() as session:
        # The audit is printed from a dry pass first, whatever the flag: the notification rows
        # it reads cascade with the deleted events.
        _report(await prune(session, apply=False))
        if args.apply:
            pruned = await prune(session, apply=True)
            for table, count in pruned.deleted.items():
                log.info("deleted %d %s rows", count, table)
            log.info("rewrote %d mixed cards", pruned.updated)
        else:
            log.info("dry run — nothing written; re-run with --apply")


if __name__ == "__main__":
    asyncio.run(main())
