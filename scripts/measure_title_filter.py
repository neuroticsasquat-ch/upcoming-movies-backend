"""Measure the per-film title-token filter's false-drop rate against the labeled
validation set. Read-only — records nothing. Run in the container:

    task shell
    python scripts/measure_title_filter.py

For each candidate min_ratio it reports how many genuinely-'about' stories the filter
would wrongly drop (false drops). Tune the config default to keep this ~= 0."""

import asyncio

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.db import SessionLocal
from upmovies.link.validation import load_validation_set
from upmovies.news.title_match import title_matches

VALIDATION_PATH = "tests/fixtures/link/validation_set.json"
RATIOS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]


async def main() -> None:
    items = [
        it
        for it in load_validation_set(VALIDATION_PATH)
        if it.relation == "about" and it.expected_film_tmdb_id is not None
    ]
    ids = {it.expected_film_tmdb_id for it in items}
    async with SessionLocal() as s:
        rows = (
            await s.execute(select(Film.tmdb_id, Film.title).where(Film.tmdb_id.in_(ids)))
        ).all()
    title_by_id = {tid: title for tid, title in rows}
    usable = [it for it in items if it.expected_film_tmdb_id in title_by_id]
    skipped = len(items) - len(usable)
    print(f"about-items={len(items)} usable={len(usable)} skipped_not_in_roster={skipped}")
    print(f"{'min_ratio':>10} {'false_drops':>12} {'rate':>8}")
    for r in RATIOS:
        drops = sum(
            1
            for it in usable
            if not title_matches(title_by_id[it.expected_film_tmdb_id], it.title, min_ratio=r)
        )
        rate = drops / len(usable) if usable else 0.0
        print(f"{r:>10.2f} {drops:>12} {rate:>7.1%}")


if __name__ == "__main__":
    asyncio.run(main())
