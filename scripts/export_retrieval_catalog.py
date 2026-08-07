"""Export the films `validation_set.json` labels into the retrieval oracle's catalog.

Run in the container:
`task shell` then
`python scripts/export_retrieval_catalog.py > tests/fixtures/link/retrieval_catalog.json`.

`tests/integration/link/retrieval/test_recall_oracle.py` scores retrieval against this
file rather than the dev database, so the recall floor means the same thing on every
machine and does not move as the local catalog is re-ingested. Only what retrieval
*scores* on is exported — title, original title, alternative titles. Release dates are
deliberately left out: the test dates every film into the future so the fixture's films
stay active as the real ones reach release.

Re-run this when the validation set gains a labeled film; the test fails with that
instruction when the two drift apart. Unlike the other fixture exporters here, its output
is committed — the point is a catalog that does not depend on this database.
"""

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from upmovies.catalog.models import Film, FilmAlternativeTitle
from upmovies.db import SessionLocal
from upmovies.link.validation import load_validation_set

VALIDATION_SET = Path("tests/fixtures/link/validation_set.json")


async def main() -> None:
    tmdb_ids = sorted(
        {
            item.expected_film_tmdb_id
            for item in load_validation_set(VALIDATION_SET)
            if item.expected_film_tmdb_id is not None
        }
    )
    async with SessionLocal() as s:
        films = (
            (await s.execute(select(Film).where(Film.tmdb_id.in_(tmdb_ids)).order_by(Film.tmdb_id)))
            .scalars()
            .all()
        )
        alt_titles: dict[int, list[str]] = {}
        for film_id, title in (
            await s.execute(
                select(FilmAlternativeTitle.film_id, FilmAlternativeTitle.title)
                .where(FilmAlternativeTitle.film_id.in_([f.id for f in films]))
                .distinct()
                # Sorted by title rather than by row id so re-exporting after a re-ingest
                # — which renumbers the child rows — produces the same file. `distinct`
                # for the same reason: TMDB returns one row per country, so a title used
                # in several territories arrives repeated, and retrieval scores a film's
                # titles as a set anyway.
                .order_by(FilmAlternativeTitle.title)
            )
        ).all():
            alt_titles.setdefault(film_id, []).append(title)

    missing = set(tmdb_ids) - {f.tmdb_id for f in films}
    if missing:
        raise SystemExit(f"labeled films absent from catalog.film: {sorted(missing)}")

    print(
        json.dumps(
            [
                {
                    "tmdb_id": film.tmdb_id,
                    "title": film.title,
                    "original_title": film.original_title,
                    "alternative_titles": alt_titles.get(film.id, []),
                }
                for film in films
            ],
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
