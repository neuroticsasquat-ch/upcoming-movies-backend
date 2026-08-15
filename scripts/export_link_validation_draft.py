"""Draft a stratified sample of stories for hand-labeling.

Run in the container:

    task shell
    python scripts/export_link_validation_draft.py --target 4000 \
        --as-of 2026-08-08 --exclude tests/fixtures/link/validation_set.json \
        > tests/fixtures/link/validation_draft.json

Then `propose_validation_labels.py` fills in candidate labels and a human corrects them
(see `tests/fixtures/link/README.md`).

**It samples rather than dumps.** The original version exported *every* story in a 45-day
window. That was reasonable at a few hundred rows and is not at 98k: the retained corpus now
holds roughly that many stories, and no one is hand-reviewing four orders of magnitude more
rows than the labeled set needs. `--target` says how many rows to draft and `source_quotas`
splits them across feed sources in proportion, so the draft's source mix matches the corpus
instead of matching whatever an unordered `LIMIT` happened to catch.

**Selection is deterministic given `--seed`.** Rows are ordered by `md5(seed || url)` inside
each source, so the same seed against the same corpus drafts the same rows — the labeled set
stays reproducible from its recipe rather than only from its output. It is a hash rather than
`ORDER BY random()` for exactly that reason.

**`--as-of` is the pin, and it belongs here too.** The fixture is scored against the catalog
as it stood on its `as_of_date`, so drafting a story published *after* that date asks the
linker to reason about news from its own future. Bounding the draft at the pin is what makes
"no row is mis-served by the pin" structural instead of a caveat (spec §5.1a).
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, time

from sqlalchemy import func, select

from upmovies.db import SessionLocal
from upmovies.link.validation import load_validation_set
from upmovies.news.models import Story

DEFAULT_TARGET = 4000
DEFAULT_SEED = "neu-1012"


def source_quotas(counts: Mapping[str, int], target: int) -> dict[str, int]:
    """Split `target` rows across sources in proportion to `counts`.

    Largest-remainder allocation: each source gets the floor of its proportional share and
    the rounding residue goes to the largest remainders, ties broken by source name so two
    runs agree. A source whose proportional share is below one row gets zero — proportional
    stratification keeps the sources it drafts in proportion, it does not guarantee that
    every source is drafted. Raising `target` is the remedy.

    A `target` at or above the corpus takes the whole corpus, so no source is ever asked for
    more rows than it has."""
    total = sum(counts.values())
    if not counts or target <= 0:
        return {source: 0 for source in counts}
    if target >= total:
        return dict(counts)

    exact = {source: n * target / total for source, n in counts.items()}
    quotas = {source: int(share) for source, share in exact.items()}
    residue = target - sum(quotas.values())
    ranked = sorted(counts, key=lambda s: (-(exact[s] - quotas[s]), s))
    for source in ranked[:residue]:
        quotas[source] += 1
    return quotas


def _published():
    """The date a story is windowed by — its publication date, or its fetch date when the
    feed gave none. The same coalesce the rest of the corpus tooling windows on."""
    return func.coalesce(Story.published_at, Story.fetched_at)


async def _source_counts(session, *, cutoff: datetime, exclude_urls: set[str]) -> dict[str, int]:
    stmt = select(Story.source, func.count()).where(_published() <= cutoff).group_by(Story.source)
    if exclude_urls:
        stmt = stmt.where(Story.url.not_in(exclude_urls))
    return {source: n for source, n in (await session.execute(stmt)).all()}


async def _draw(
    session, *, source: str, quota: int, cutoff: datetime, seed: str, exclude_urls: set[str]
) -> list[Story]:
    """The `quota` rows of `source` whose `md5(seed || url)` sorts first.

    A stable hash rather than `random()`: the draw has to be reproducible from the seed, and
    it has to stay reproducible when the corpus grows underneath it."""
    if quota <= 0:
        return []
    stmt = (
        select(Story)
        .where(Story.source == source, _published() <= cutoff)
        .order_by(func.md5(func.concat(seed, Story.url)))
        .limit(quota)
    )
    if exclude_urls:
        stmt = stmt.where(Story.url.not_in(exclude_urls))
    return list((await session.execute(stmt)).scalars().all())


def _draft_row(story: Story) -> dict:
    return {
        "url": story.url,
        "source": story.source,
        "title": story.title,
        "summary": (story.raw.get("summary", "") if isinstance(story.raw, dict) else ""),
        "relation": "TODO",  # about | mention | none
        "expected_film_tmdb_id": None,  # required iff relation == about
        "event_type": None,  # required iff relation == about
        "event_group": None,  # optional cluster label
    }


async def main(args: argparse.Namespace) -> None:
    exclude_urls: set[str] = set()
    if args.exclude:
        exclude_urls = {item.url for item in load_validation_set(args.exclude).items}
        print(f"excluding {len(exclude_urls)} already-labeled urls", file=sys.stderr)

    cutoff = datetime.combine(args.as_of, time.max, tzinfo=UTC)
    async with SessionLocal() as session:
        counts = await _source_counts(session, cutoff=cutoff, exclude_urls=exclude_urls)
        quotas = source_quotas(counts, args.target)
        rows: list[Story] = []
        for source in sorted(quotas):
            rows.extend(
                await _draw(
                    session,
                    source=source,
                    quota=quotas[source],
                    cutoff=cutoff,
                    seed=args.seed,
                    exclude_urls=exclude_urls,
                )
            )

    _report(counts, quotas, len(rows), args)
    print(json.dumps([_draft_row(row) for row in rows], indent=2, ensure_ascii=False))


def _report(
    counts: Mapping[str, int], quotas: Mapping[str, int], drawn: int, args: argparse.Namespace
) -> None:
    print(
        f"corpus {sum(counts.values())} stories <= {args.as_of} across {len(counts)} sources"
        f" | target {args.target} | drawn {drawn} | seed {args.seed!r}",
        file=sys.stderr,
    )
    for source in sorted(quotas, key=lambda s: -counts[s]):
        if quotas[source]:
            print(f"  {source:38} {quotas[source]:5} / {counts[source]}", file=sys.stderr)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET, help="rows to draft")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date.today(),
        help="draft only stories published on or before this date (the fixture's pin)",
    )
    parser.add_argument("--seed", default=DEFAULT_SEED, help="hash seed; same seed, same draw")
    parser.add_argument(
        "--exclude",
        help="a validation set whose urls are already labeled and must not be drafted again",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
