"""Dump recent stories into a draft validation file for hand-labeling. Run in the
container: `task shell` then `python scripts/export_link_validation_draft.py > \
tests/fixtures/link/validation_draft.json`. Fill in relation / expected_film_tmdb_id / \
event_type by hand, then save as validation_set.json."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from upmovies.db import SessionLocal
from upmovies.news.models import Story

RECENCY_DAYS = 45


async def main() -> None:
    cutoff = datetime.now(UTC) - timedelta(days=RECENCY_DAYS)
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Story)
                .where(func.coalesce(Story.published_at, Story.fetched_at) >= cutoff)
                .order_by(Story.source, Story.title)
            )
        ).scalars().all()
    draft = [
        {
            "url": row.url,
            "source": row.source,
            "title": row.title,
            "summary": (row.raw.get("summary", "") if isinstance(row.raw, dict) else ""),
            "relation": "TODO",  # about | mention | none
            "expected_film_tmdb_id": None,  # required iff relation == about
            "event_type": None,  # required iff relation == about
            "event_group": None,  # optional cluster label
        }
        for row in rows
    ]
    print(json.dumps(draft, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
