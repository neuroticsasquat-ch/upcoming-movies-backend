"""Backfill credit-removal events for detachments that predate the NEU-1200 deployment.

Cards every `film_credit_change` row with `change='removed'` that attaches to a film with a
prior visible attachment card. No lookback window — reads all history. Idempotent via
`_already_carded` + `uq_event_catalog_change`.

Run once at ship via `task shell`:

    python scripts/backfill_credit_removals.py

Safe to re-run; the carding logic skips already-carded groups.
"""

import asyncio
from datetime import UTC, datetime

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep import run_credit_detachment_events
from upmovies.ingest.sweep.credit_events import (
    _card_detachment_group,
    group_detachments,
    load_detachment_backlog,
)


async def main() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    created = 0
    skipped = 0
    failed = 0

    async with SessionLocal() as s:
        detachments = await load_detachment_backlog(s)
    groups = group_detachments(detachments)
    print(
        f"backfill_credit_removals: {len(detachments)} detachments "
        f"in {len(groups)} groups"
    )

    for group in groups:
        try:
            async with SessionLocal() as s:
                carded = await _card_detachment_group(
                    s,
                    group=group,
                    now=now,
                    dwell_days=settings.sweep_credit_dwell_days,
                )
                if carded:
                    created += 1
                else:
                    skipped += 1
                await s.commit()
        except Exception:
            failed += 1
            print(f"  failed: film {group.film_id} at {group.changed_at}")

    print(
        f"backfill_credit_removals: {created} created, {skipped} skipped, {failed} failed"
    )


if __name__ == "__main__":
    asyncio.run(main())
