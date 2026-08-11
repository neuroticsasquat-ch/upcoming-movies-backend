"""Re-render deterministic status-event summaries after a template change.

A deterministic summary is written once, when the event is carded, and the sweep skips events
it has already carded — so changing a template in `synthesize.deterministic` only affects
events carded *after* the change. Without this, rewording leaves the live feed showing both
phrasings side by side indefinitely.

Safe to re-run, and safe by construction rather than by care:

- It re-renders from the same `render_summary` the writer uses, so the output is whatever the
  current template says. There is no second copy of the wording here to drift.
- The change is reconstructed from the **event type**, not from a stored payload:
  `production_start` ⇄ `In Production`, `production_wrap` ⇄ `Post Production`, inverted from
  `news.catalog_events.STATUS_EVENT_TYPES` so the two cannot disagree.
- **Human edits are never overwritten.** Rows with `edited_at` set are skipped, matching the
  `replace_when` guard the deterministic writer already applies.
- Only `provenance = 'catalog'` rows with the deterministic model are touched; LLM-written
  summaries for story-sourced events are a different path and are left alone.

Scope is deliberately narrow: **status events only.** Release-date bodies are rendered from
`film_release_date_change` rows and would need that history re-read to reconstruct, which is a
different job.

    task shell
    python scripts/rerender_status_summaries.py            # report only
    python scripts/rerender_status_summaries.py --apply
"""

import argparse
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.db import SessionLocal
from upmovies.news.catalog_events import STATUS_EVENT_TYPES
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import (
    DETERMINISTIC_MODEL,
    TEMPLATE_VERSION,
    StatusChanged,
    render_summary,
)

log = logging.getLogger("rerender_status_summaries")

# `production_start` → `In Production`, and so on. Inverted from the forward map so a new
# status added there cannot be silently missed here.
EVENT_TYPE_TO_STATUS: dict[str, str] = {v: k for k, v in STATUS_EVENT_TYPES.items()}


async def rerender(session: AsyncSession, *, apply: bool) -> list[tuple[str, str]]:
    """Re-render every unedited deterministic status summary. Returns (old, new) for each row
    whose body actually changes — an unchanged render is not reported as work."""
    rows = (
        await session.execute(
            select(EventSummary, Event.event_type)
            .join(Event, Event.id == EventSummary.event_id)
            .where(
                Event.event_type.in_(EVENT_TYPE_TO_STATUS),
                Event.provenance == "catalog",
                EventSummary.model == DETERMINISTIC_MODEL,
                # A body someone edited by hand is theirs, not the template's.
                EventSummary.edited_at.is_(None),
            )
        )
    ).all()

    changed: list[tuple[str, str]] = []
    for summary, event_type in rows:
        body = render_summary(StatusChanged(new_status=EVENT_TYPE_TO_STATUS[event_type]))
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
