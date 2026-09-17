"""Backfill the supersession marks for `credit_removed` cards written before NEU-1347.

Before the supersession write shipped, a removal carded without touching the attachment card
it corrected, so those originals still read `published`. This walks every removal card in
the ledger and applies the same write the sweep now performs at carding time
(`supersede_prior_attachment_cards`): the most recent published attachment card for each
named person, occurred before the removal, is set `superseded` and linked to it.

Forward-only and idempotent, mirroring `backfill_credit_removals.py`: a removal card that
already supersedes something is skipped, nothing is un-marked, and a removal with no prior
attachment card is left alone.

Run once at ship via `task shell`:

    python scripts/backfill_credit_supersessions.py

Safe to re-run.
"""

import asyncio
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.db import SessionLocal
from upmovies.ingest.sweep.credit_events import supersede_prior_attachment_cards
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.news.catalog_events import CREDIT_REMOVED_EVENT_TYPE
from upmovies.news.models import Event


@dataclass
class BackfillResult:
    """What one pass over the removal cards read and wrote."""

    removals_read: int = 0
    superseded: int = 0
    """Attachment cards marked."""
    skipped: int = 0
    """Removal cards that already superseded something — an earlier pass, or the sweep."""
    failed: int = 0


async def _already_supersedes(session: AsyncSession, removal_id) -> bool:
    marked = exists().where(Event.superseded_by == removal_id)
    return bool((await session.execute(select(marked))).scalar())


async def backfill(session_factory: SessionFactory) -> BackfillResult:
    """Apply the supersession write to every removal card that has not had it. One session
    per removal, so one unwritable card never rolls back the others."""
    result = BackfillResult()
    async with session_factory() as s:
        removal_ids = (
            (
                await s.execute(
                    select(Event.id)
                    .where(Event.event_type == CREDIT_REMOVED_EVENT_TYPE)
                    .order_by(Event.occurred_at, Event.created_at)
                )
            )
            .scalars()
            .all()
        )
    result.removals_read = len(removal_ids)

    for removal_id in removal_ids:
        try:
            async with session_factory() as s:
                if await _already_supersedes(s, removal_id):
                    result.skipped += 1
                    continue
                removal = await s.get_one(Event, removal_id)
                result.superseded += await supersede_prior_attachment_cards(s, removal=removal)
                await s.commit()
        except Exception:
            result.failed += 1
            print(f"  failed: removal card {removal_id}")
    return result


async def main() -> None:
    result = await backfill(SessionLocal)
    print(
        f"backfill_credit_supersessions: {result.removals_read} removal cards read, "
        f"{result.superseded} attachment cards superseded, "
        f"{result.skipped} already done, {result.failed} failed"
    )


if __name__ == "__main__":
    asyncio.run(main())
