"""Repair events whose `occurred_at` predates their own earliest surviving story.

Fixes the drift NEU-968 left behind: `occurred_at` was stamped once at event creation and
never recomputed, so events repaired by the Google-pause / blocked-domain cleanups kept
pointing at stories that had been purged. Because `occurred_at` orders the public film
timeline, those events display and sort by a date no story supports. Measured on production
2026-08-05: 31 of 199 events, drifting by up to 19 days.

Run once after the repair fix deploys; it is idempotent. Dry-run by default.

    task shell
    python scripts/backfill_event_occurred_at.py          # report only
    python scripts/backfill_event_occurred_at.py --apply  # write"""

import asyncio
import sys

from upmovies.db import SessionLocal
from upmovies.link.event_repair import repair_drifted_occurred_at


async def main(apply: bool) -> None:
    async with SessionLocal() as session:
        report = await repair_drifted_occurred_at(session, apply=apply)
        if apply:
            await session.commit()
    verb = "repaired" if apply else "would repair"
    print(
        f"backfill_event_occurred_at: {verb} {report.events_repaired} event(s); "
        f"max drift {report.max_drift_days} day(s)"
    )
    if not apply and report.events_repaired:
        print("dry run — re-run with --apply to write")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv[1:]))
