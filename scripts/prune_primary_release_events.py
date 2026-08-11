"""Remove catalog release-date events the displayable rule would never have created (NEU-1121).

One-off data repair, not a migration: it edits rows rather than schema, it is only meaningful
against production, and Alembic running it on every environment would be wrong.

## What it removes, and why all of them

Every `news.event` with `event_type = 'release_date'` and `provenance = 'catalog'` was carded
from `catalog.film.release_date` — TMDB's *primary* date, the earliest release in any country
of any type — while the film page lists US-or-origin theatrical dates only. On the 2026-08-11
sweep that produced cards naming dates the page never showed:

- *Avengers: Secret Wars* — page "Wide, Dec 17 2027" (US), card "moved to 15 September 2027",
  primary `2027-12-15` (France). Three dates, none agreeing.
- *Cliffhanger* — origin US, only release row German, so the page shows no release dates at
  all; it got two cards, mutually contradictory.
- 21 of 56 were for films with no displayable theatrical date anywhere.

The remaining 35 are not salvageable either: the event records a movement of the *primary*
date, and there is no stored history saying which displayable date (if any) moved with it.
Rewriting them would mean inventing a subject. They go, and correct ones are carded from
`catalog.film_release_date_change` from the next sweep onward.

**Story-sourced release-date events are untouched** — `provenance = 'story'` comes from `link`
reading a trade report and is a different, working path. So are `production_start`,
`production_wrap` and credit events: unaffected by NEU-1121.

## Deliberately unlike NEU-446

That ticket chose forward-only, letting a wrong regional event age out. The scale is different
here — a fifth of a day's feed, on films whose pages contradict the card — so these are
removed rather than left to age.

Run it in the container, dry by default:

    task shell
    python scripts/prune_primary_release_events.py            # report only
    python scripts/prune_primary_release_events.py --apply    # delete
"""

import argparse
import asyncio
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.db import SessionLocal
from upmovies.news.models import Event, EventStory, EventSummary

log = logging.getLogger("prune_primary_release_events")

TARGET = (Event.event_type == "release_date", Event.provenance == "catalog")


async def _report(session: AsyncSession) -> list[tuple[str, str, int]]:
    """Affected films, most cards first — the before picture, printed either way."""
    rows = (
        await session.execute(
            select(Film.title, Film.slug, func.count(Event.id))
            .join(Event, Event.film_id == Film.id)
            .where(*TARGET)
            .group_by(Film.title, Film.slug)
            .order_by(func.count(Event.id).desc(), Film.title)
        )
    ).all()
    return [(title, slug, n) for title, slug, n in rows]


async def prune(session: AsyncSession, *, apply: bool) -> int:
    """Delete the targeted events and their dependants. Returns the event count.

    `event_summary` and `event_story` are cleared explicitly rather than trusted to cascade:
    a summary orphaned by a deleted event is invisible to every read path (they inner-join)
    but would still be there, and `event_story` holds the link rows a later re-cluster reads.
    """
    ids = list((await session.execute(select(Event.id).where(*TARGET))).scalars().all())
    if not ids:
        return 0
    if apply:
        await session.execute(delete(EventStory).where(EventStory.event_id.in_(ids)))
        await session.execute(delete(EventSummary).where(EventSummary.event_id.in_(ids)))
        await session.execute(delete(Event).where(Event.id.in_(ids)))
        await session.commit()
    return len(ids)


async def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="actually delete (default is report only)"
    )
    args = parser.parse_args(argv)

    async with SessionLocal() as session:
        affected = await _report(session)
        total = sum(n for _, _, n in affected)
        log.info("%d catalog release-date events across %d films", total, len(affected))
        for title, slug, n in affected[:25]:
            log.info("  %-45s %-40s %d", title[:45], slug or "-", n)
        if len(affected) > 25:
            log.info("  ... and %d more films", len(affected) - 25)

        deleted = await prune(session, apply=args.apply)
        if args.apply:
            log.info("deleted %d events", deleted)
        else:
            log.info("dry run — %d events would be deleted; re-run with --apply", deleted)


if __name__ == "__main__":
    asyncio.run(main())
