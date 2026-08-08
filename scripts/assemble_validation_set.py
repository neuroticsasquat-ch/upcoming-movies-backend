"""Assemble a re-pinned, enlarged validation set from the old labels plus new proposals.

    task shell
    python scripts/assemble_validation_set.py --as-of 2026-08-08 \
        --carry tests/fixtures/link/validation_set.json \
        --candidates tests/fixtures/link/validation_candidates.json \
        > /tmp/validation_set.draft.json

The output is a *starting point for review*, not a fixture. Every row it emits from
`--candidates` carries a model's proposal and has to be read before it is ground truth (see
`tests/fixtures/link/README.md`); this script only decides which rows are worth reading and
how the two sources are reconciled.

**Why the old set is carried rather than replaced.** Its rows are hand-made ground truth and
the most expensive thing the project owns. Four of them are pinned by name in regression
tests (NEU-989, -461, -445, -1009) and nine are synthetic rows written to exercise the
production-news axis where the real corpus was thin.

**Re-pinning is what makes carrying non-trivial.** The pin moves forward to the labeling
date, which is the only date at which no story in the corpus is in the fixture's future and
at which `films_ingested_after` is empty. But a later pin excludes more films: everything
that released between the old pin and the new one leaves the roster. An `about` row naming
one of those is unscoreable — both link paths filter the film out, so the row reads as a
recall failure of whichever path is under test, and `validate_linking._check_coverage` aborts
the run rather than report it. Those rows are **demoted to `none`**, which is not a
convenience: `none` means "not about any film tracked *at labeling time*", and at the new pin
that is exactly true of them. They become hard negatives — real movie news, lexically strong
against a title the roster no longer holds — and they are the first rows this fixture has
ever had that exercise scope filtering at all (spec §5.1a: at the 2026-07-01 pin
`active_film_clause` excluded nothing).
"""

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.db import SessionLocal

# NEU-1004 deletes `build_roster`. Both this script and the proposer read the roster to decide
# what may be labeled, so both follow it to the ported implementation — one import site each,
# and `roster_tmdb_ids` shared rather than copied, so the deletion surface is as small as the
# job allows.
from upmovies.link.roster import build_roster, roster_tmdb_ids

# `about : none` in the assembled set. Held near the 94:193 the 302-row fixture carried so
# the new numbers stay readable against §5.7/§5.8's, which were taken at that mix.
DEFAULT_NONE_RATIO = 2.0
DEFAULT_SEED = "neu-1012"

_ABOUT_ONLY_FIELDS = ("event_type", "event_group", "is_production_news", "exclusion_category")


def demote_unrostered(row: dict, rostered: set[int]) -> dict:
    """An `about` row whose film the pin excludes becomes an `untracked_film` `none` row.

    Every `about`-only field is cleared, not just the film id: `ValidationItem` rejects a
    non-`about` row that still carries `is_production_news` or `exclusion_category`, and a
    fixture that fails to load is a poor way to discover a half-finished demotion."""
    if row.get("relation") != "about" or row.get("expected_film_tmdb_id") in rostered:
        return row
    return to_untracked_none(row)


def to_untracked_none(row: dict) -> dict:
    """Rewrite an `about` row as an `untracked_film` `none` row, clearing every `about`-only
    field. `ValidationItem` rejects a non-`about` row that still carries `is_production_news`
    or `exclusion_category`, so a half-finished demotion fails to load rather than mislabelling
    quietly — but discovering that from a 1,000-row fixture is a bad way to spend an hour."""
    demoted = dict(row)
    demoted["relation"] = "none"
    demoted["expected_film_tmdb_id"] = None
    for field in _ABOUT_ONLY_FIELDS:
        demoted[field] = None
    demoted["untracked_film"] = True
    return demoted


def select_none_rows(rows: Sequence[dict], *, target: int, seed: str) -> list[dict]:
    """A deterministic subsample of `rows`, ordered by `md5(seed || url)`.

    The same seed against the same candidates picks the same rows, so the fixture is
    reproducible from its recipe and not only from its output — the same reason the draft
    sampler hashes rather than calling `random()`."""
    if target >= len(rows):
        return list(rows)
    ranked = sorted(rows, key=lambda r: hashlib.md5(f"{seed}{r['url']}".encode()).hexdigest())
    return ranked[:target]


def summarize(rows: Iterable[dict]) -> dict[str, int]:
    """Class counts, and the two different populations they feed.

    **`about` is what sets the gate's resolution.** `compute_link_metrics` scores a true
    positive as any `about` row linked to its labeled film, whether or not the row is
    production news — a not-production-news row that leaks is a true positive *and* a
    news-value false positive, scored on separate axes. So the true-positive ceiling is the
    whole `about` count, and growing it is what makes one false positive worth less of a
    precision point.

    `linkable_about` is the *news-value* population: the rows expected to link, which is what
    `compute_news_value_metrics` scores recall against. Reported because it moves independently
    — a corpus can gain `about` rows while gaining almost no linkable ones."""
    rows = list(rows)
    about = [r for r in rows if r.get("relation") == "about"]
    return {
        "total": len(rows),
        "about": len(about),
        "linkable_about": sum(1 for r in about if r.get("is_production_news") is not False),
        "mention": sum(1 for r in rows if r.get("relation") == "mention"),
        "none": sum(1 for r in rows if r.get("relation") == "none"),
        "todo": sum(1 for r in rows if r.get("relation") == "TODO"),
    }


async def _rostered_tmdb_ids(as_of: date) -> set[int]:
    async with SessionLocal() as session:
        roster = await build_roster(session, as_of=as_of)
        tmdb_by_film_id = {
            f.id: f.tmdb_id for f in (await session.execute(select(Film))).scalars().all()
        }
    return roster_tmdb_ids(roster.entries, tmdb_by_film_id)


async def main(args: argparse.Namespace) -> None:
    rostered = await _rostered_tmdb_ids(args.as_of)
    print(f"roster at {args.as_of}: {len(rostered)} films", file=sys.stderr)

    carried_raw = json.loads(Path(args.carry).read_text())
    carried_rows = carried_raw["items"] if isinstance(carried_raw, dict) else carried_raw
    carried = [demote_unrostered(row, rostered) for row in carried_rows]
    # Counted by comparing input to output rather than by testing `untracked_film` on the
    # result: the carried set already holds `untracked_film` rows of its own, so carrying the
    # flag is not the same as having been demoted by this run.
    demoted = sum(
        1
        for old, new in zip(carried_rows, carried, strict=True)
        if old.get("relation") == "about" and new.get("relation") == "none"
    )
    print(f"carried {len(carried)} rows, demoted {demoted} to untracked none", file=sys.stderr)

    proposed = json.loads(Path(args.candidates).read_text())
    seen = {row["url"] for row in carried}
    proposed = [row for row in proposed if row["url"] not in seen]

    positives = [row for row in proposed if row["relation"] in ("about", "mention")]
    # An `about` proposal the roster guard stripped is unusable as-is: the schema requires a
    # film id. Keep it for review rather than dropping it — it is usually a real story about
    # an untracked film, which is the `untracked_film` coverage signal.
    positives = [
        to_untracked_none(row)
        if row["relation"] == "about" and row["expected_film_tmdb_id"] is None
        else row
        for row in positives
    ]

    about_total = sum(1 for r in carried + positives if r.get("relation") == "about")
    none_have = sum(1 for r in carried if r.get("relation") == "none") + sum(
        1 for r in positives if r.get("relation") == "none"
    )
    none_want = max(0, round(about_total * args.none_ratio) - none_have)
    negatives = select_none_rows(
        [row for row in proposed if row["relation"] == "none"],
        target=none_want,
        seed=args.seed,
    )

    items = carried + positives + negatives
    counts = summarize(items)
    print(
        f"assembled {counts['total']} rows: {counts['about']} about "
        f"({counts['linkable_about']} linkable), {counts['mention']} mention, "
        f"{counts['none']} none, {counts['todo']} TODO",
        file=sys.stderr,
    )
    print(
        json.dumps(
            {"as_of_date": args.as_of.isoformat(), "items": items}, indent=2, ensure_ascii=False
        )
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True, help="the new pin")
    parser.add_argument("--carry", required=True, help="the existing labeled set")
    parser.add_argument("--candidates", required=True, help="proposed labels for the new draft")
    parser.add_argument("--none-ratio", type=float, default=DEFAULT_NONE_RATIO)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
