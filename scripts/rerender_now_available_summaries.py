"""Re-render deterministic now_available summaries after the rent and buy bodies dropped stores.

A deterministic summary is written once, when the event is carded, so rewording a template in
`synthesize.deterministic` only reaches cards raised after the change. Without this, the feed
shows "Available to rent on Apple TV and Prime Video." beside "Available to rent." indefinitely.

Safe to re-run, and safe by construction rather than by care, for the reasons
`rerender_status_summaries` is:

- It re-renders from the same `render_summary` the poll uses — no second copy of the wording.
- The change is reconstructed from stored facts, not from the old body: the **types** from the
  event's `US:rent`-style `subject_key` tokens, and the **streaming services** from the
  `availability_first_seen` rows the same observation inserted — they share the event's
  `occurred_at` (the poll dates the card to `first_seen_at`), and their ids follow the order the
  poll saw the services in. Rent and buy need no services; the body no longer names any.
- A streaming card whose ledger rows cannot be found is **left alone** and counted, never
  rendered with an empty list of services.
- **Human edits are never overwritten** (`edited_at` set), and only `provenance = 'catalog'`
  rows with the deterministic model are touched.

    task shell
    python scripts/rerender_now_available_summaries.py            # report only
    python scripts/rerender_now_available_summaries.py --apply
"""

import argparse
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import AvailabilityFirstSeen, WatchProvider
from upmovies.db import SessionLocal
from upmovies.news.catalog_events import NOW_AVAILABLE_EVENT_TYPE
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import (
    DETERMINISTIC_MODEL,
    TEMPLATE_VERSION,
    AvailableOn,
    NowAvailable,
    render_summary,
)

log = logging.getLogger("rerender_now_available_summaries")


async def _streaming_providers(session: AsyncSession, event: Event, region: str) -> tuple[str, ...]:
    """The services the observation behind `event` first saw the film streaming on."""
    rows = await session.execute(
        select(WatchProvider.name)
        .join(AvailabilityFirstSeen, AvailabilityFirstSeen.provider_id == WatchProvider.id)
        .where(
            AvailabilityFirstSeen.film_id == event.film_id,
            AvailabilityFirstSeen.region == region,
            AvailabilityFirstSeen.monetization_type == "flatrate",
            AvailabilityFirstSeen.first_seen_at == event.occurred_at,
        )
        .order_by(AvailabilityFirstSeen.id)
    )
    return tuple(rows.scalars())


async def _change_for(session: AsyncSession, event: Event) -> NowAvailable | None:
    """The change `event` was carded for, rebuilt from its tokens and the ledger — or None when
    a streaming token's ledger rows cannot be found, or the event carries no tokens at all."""
    offers: list[AvailableOn] = []
    for token in event.subject_key or []:
        region, kind = token.split(":", 1)
        providers: tuple[str, ...] = ()
        if kind == "flatrate":
            providers = await _streaming_providers(session, event, region)
            if not providers:
                return None
        offers.append(AvailableOn(monetization_type=kind, providers=providers))
    return NowAvailable(offers=tuple(offers)) if offers else None


async def rerender(session: AsyncSession, *, apply: bool) -> list[tuple[str, str]]:
    """Re-render every unedited deterministic now_available summary. Returns (old, new) for
    each row whose body actually changes — an unchanged render is not reported as work."""
    rows = (
        await session.execute(
            select(EventSummary, Event)
            .join(Event, Event.id == EventSummary.event_id)
            .where(
                Event.event_type == NOW_AVAILABLE_EVENT_TYPE,
                Event.provenance == "catalog",
                EventSummary.model == DETERMINISTIC_MODEL,
                # A body someone edited by hand is theirs, not the template's.
                EventSummary.edited_at.is_(None),
            )
            .order_by(Event.occurred_at)
        )
    ).all()

    changed: list[tuple[str, str]] = []
    for summary, event in rows:
        change = await _change_for(session, event)
        if change is None:
            log.warning(
                "left alone, no ledger rows to rebuild from: %s %r", event.id, summary.summary
            )
            continue
        body = render_summary(change)
        if body == summary.summary:
            continue
        changed.append((summary.summary, body))
        if apply:
            summary.summary = body
            summary.prompt_version = TEMPLATE_VERSION
    if apply and changed:
        await session.commit()
    return changed


async def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="actually rewrite (default is report only)"
    )
    args = parser.parse_args(argv)

    async with SessionLocal() as session:
        changed = await rerender(session, apply=args.apply)
        for old, new in changed[:10]:
            log.info("  %r -> %r", old, new)
        if len(changed) > 10:
            log.info("  ... and %d more", len(changed) - 10)
        verb = "rewrote" if args.apply else "would rewrite"
        log.info("%s %d summaries", verb, len(changed))


if __name__ == "__main__":
    asyncio.run(main())
