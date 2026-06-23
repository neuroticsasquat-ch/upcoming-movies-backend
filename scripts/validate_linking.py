"""Measure Stage-1 linking accuracy against the labeled fixture using the real model.
Run in the container with a real key in .env:
    task shell
    python scripts/validate_linking.py [tests/fixtures/link/validation_set.json]

Prints precision / recall / F1 and the confusion counts. Costs a small amount of API
tokens. Use it to set `link_confidence_floor` and tune the linker prompt; record the
numbers in the design spec."""

import asyncio
import sys
from uuid import uuid4

from sqlalchemy import select

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.catalog.models import Film
from upmovies.link.linker import link_story_batch
from collections.abc import Iterable

from upmovies.link.metrics import compute_link_metrics, compute_news_value_metrics
from upmovies.link.roster import build_roster
from upmovies.link.validation import ValidationItem, load_validation_set
from upmovies.llm.client import AnthropicClient
from upmovies.news.models import Story

DEFAULT_FIXTURE = "tests/fixtures/link/validation_set.json"


def news_value_rows(
    items_with_linked: Iterable[tuple[ValidationItem, bool]],
) -> list[tuple[bool, bool | None, str | None]]:
    """Map (item, linked) pairs to the news-value scorer's row shape, keeping 'about' only."""
    return [
        (linked, it.is_production_news, it.exclusion_category)
        for it, linked in items_with_linked
        if it.relation == "about"
    ]


async def main(path: str) -> None:
    settings = get_settings()
    items = load_validation_set(path)
    n_about = sum(1 for it in items if it.relation == "about")
    print(f"fixture: {len(items)} items total, {n_about} 'about' (linkable), {len(items) - n_about} mention/none")

    async with SessionLocal() as s:
        roster = await build_roster(s)
        tmdb_by_film_id = dict(
            (row.id, row.tmdb_id) for row in (await s.execute(select(Film))).scalars().all()
        )

    expected_ids = {it.expected_film_tmdb_id for it in items if it.expected_film_tmdb_id}
    roster_tmdb_ids = {
        tmdb_id for e in roster.entries if (tmdb_id := tmdb_by_film_id.get(e.film_id)) is not None
    }
    missing = expected_ids - roster_tmdb_ids
    if missing:
        print(f"WARNING: {len(missing)} expected film(s) are not in the current roster: {missing}")

    # Build throwaway Story objects carrying the fixture text.
    stories = [
        Story(id=uuid4(), source=it.source, url=it.url, title=it.title, raw={"summary": it.summary})
        for it in items
    ]
    item_by_story_id = {str(st.id): it for st, it in zip(stories, items, strict=True)}

    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for i in range(0, len(stories), settings.link_batch_size):
            batch = stories[i : i + settings.link_batch_size]
            await link_story_batch(
                client=client, model=settings.link_model, roster=roster,
                stories=batch, floor=settings.link_confidence_floor,
            )

    # Non-'about' items have expected_film_tmdb_id=None; correct rejections count as TN
    # (they don't affect precision/recall/F1 but are included for a more complete test).
    pairs: list[tuple[int | None, int | None]] = []
    for st in stories:
        predicted = tmdb_by_film_id.get(st.film_id) if st.film_id is not None else None
        pairs.append((predicted, item_by_story_id[str(st.id)].expected_film_tmdb_id))

    m = compute_link_metrics(pairs)
    print(f"n={len(pairs)}  floor={settings.link_confidence_floor}  model={settings.link_model}")
    print(f"TP={m.true_positives} FP={m.false_positives} FN={m.false_negatives} TN={m.true_negatives}")
    print(f"precision={m.precision:.3f}  recall={m.recall:.3f}  f1={m.f1:.3f}")

    nv = compute_news_value_metrics(
        news_value_rows((it, st.film_id is not None) for st, it in zip(stories, items, strict=True))
    )
    print("\n=== NEWS-VALUE (production-news axis, 'about' rows) ===")
    print(
        f"kept-real={nv.true_positives} kept-excluded(leak)={nv.false_positives} "
        f"dropped-real={nv.false_negatives} dropped-excluded={nv.true_negatives}"
    )
    print(f"precision={nv.precision:.3f}  recall={nv.recall:.3f}")
    if nv.leaks_by_category:
        leaks = ", ".join(f"{k}={v}" for k, v in sorted(nv.leaks_by_category.items()))
        print(f"leaks by category: {leaks}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FIXTURE))
