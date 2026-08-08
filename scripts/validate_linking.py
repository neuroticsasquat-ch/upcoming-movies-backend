"""Measure Stage-1 linking accuracy against the labeled fixture using the real model.

Run in the container with a real key in .env:

    task shell
    python scripts/validate_linking.py
    python scripts/validate_linking.py --threshold 0.5 --limit 5

**One link path.** The incumbent roster path this used to score against — and the
roster → retrieval delta that was cutover gate #3 — went with the roster itself (NEU-1004,
spec §5.5). What remains is the absolute report for the path that ships: link metrics, the
news-value split, and the candidate-coverage check that gate #3a was stated in.

**Coverage is the check that survived the gate.** 3b and 3c were deltas against the roster
and have no baseline any more; 3a asked whether retrieval offered the correct film at all,
which is a property of the retrieval stage alone and still fails loudly when it does not
(spec §5.11 records it at 534/538 on the enlarged fixture — four correct labels naming films
lexical matching cannot reach).

**This costs real API tokens.** Run it deliberately, record the numbers in the design spec,
and keep it out of CI: the zero-cost retrieval oracle test (M1) is what runs on every
commit."""

import argparse
import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import (
    Completer,
    StoryCandidates,
    link_retrieval_story_batch,
    reject_zero_candidate_stories,
    story_dek,
)
from upmovies.link.metrics import (
    LinkMetrics,
    NewsValueMetrics,
    compute_link_metrics,
    compute_news_value_metrics,
)
from upmovies.link.retrieval.health import RetrievalTally
from upmovies.link.retrieval.index import (
    CandidateIndex,
    build_candidate_index,
    indexed_tmdb_ids,
)
from upmovies.link.retrieval.select import CandidateSet, select_candidates
from upmovies.link.validation import (
    ValidationItem,
    films_ingested_after,
    load_validation_set,
)
from upmovies.llm.client import AnthropicClient, CallLog
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


def link_pairs(
    items: Sequence[ValidationItem], predicted_tmdb_ids: Sequence[int | None]
) -> list[tuple[int | None, int | None]]:
    """Zip a path's predictions onto the fixture's labels, in fixture order.

    Non-'about' items carry `expected_film_tmdb_id=None`, so a correct rejection lands as a
    true negative: it moves no precision or recall number, but it keeps the confusion counts
    a complete description of what the path did with the fixture."""
    return [
        (predicted, item.expected_film_tmdb_id)
        for item, predicted in zip(items, predicted_tmdb_ids, strict=True)
    ]


@dataclass(frozen=True)
class RetrievalCoverage:
    """Whether retrieval even offered the right film — the retrieval half of a recall loss.

    Scored over 'about' items only, and *before* the classifier: a film the model was never
    shown is a lexical miss with a lexical fix (T, the fold, an alias), while a film offered
    and not chosen is a prompt or floor problem. F1 alone cannot tell those apart, and they
    are fixed in different files.

    **This is what is left of cutover gate #3.** 3a — retrieval offered the correct film for
    every scoreable `about` item — was the only one of the three checks that measured the
    stage the project actually changed; 3b and 3c were deltas against the roster and lost
    their baseline when it was deleted (NEU-1004). `complete` is that check.

    **This is not the M1 recall oracle.** That one lives in the suite, runs on every commit
    at zero token cost, and scores a pinned catalog (spec §6).

    `out_of_catalog` holds apart expected films the catalog no longer contains, rather than
    counting them as misses: a film retrieval was never allowed to consider is a scope loss,
    not a lexical one. Against a **pinned** fixture it should be zero — the pin is chosen so
    every labeled film was active on that date, and `_check_coverage` aborts the run if it
    isn't (spec §5.1a). It stays non-zero only for an unpinned fixture read at wall clock,
    where the set outlives the release dates of the films it names."""

    hits: int
    total: int
    misses: tuple[tuple[str, int], ...]
    out_of_catalog: int = 0

    @property
    def rate(self) -> float | None:
        """Fraction of reachable linkable items whose expected film was offered.

        None when nothing was reachable — a zero there would read as total retrieval
        failure when it means the fixture and the catalog no longer overlap."""
        return self.hits / self.total if self.total else None

    @property
    def complete(self) -> bool:
        """Gate #3a: every scoreable `about` item's expected film was offered.

        One-sided and absolute — there is no baseline to beat, only a coverage gap to have
        or not have. It reads 534/538 on the enlarged fixture, so this fails on a real gap
        (spec §5.11): four correct labels naming films lexical matching cannot reach."""
        return self.hits == self.total


def retrieval_coverage(
    items: Sequence[ValidationItem],
    offered_tmdb_ids: Sequence[tuple[int, ...]],
    *,
    indexed_tmdb_ids: set[int] | None = None,
) -> RetrievalCoverage:
    """Score each 'about' item's expected film against the candidates it was offered.

    `indexed_tmdb_ids` is the active catalog retrieval could draw from; expected films
    outside it are set aside rather than scored, since no threshold or fold could have
    reached them. Omit it to score every item, which is what a hand-built index in a test
    wants."""
    hits = total = out_of_catalog = 0
    misses: list[tuple[str, int]] = []
    for item, offered in zip(items, offered_tmdb_ids, strict=True):
        expected = item.expected_film_tmdb_id
        if item.relation != "about" or expected is None:
            continue
        if indexed_tmdb_ids is not None and expected not in indexed_tmdb_ids:
            out_of_catalog += 1
            continue
        total += 1
        if expected in offered:
            hits += 1
        else:
            misses.append((item.title, expected))
    return RetrievalCoverage(
        hits=hits, total=total, misses=tuple(misses), out_of_catalog=out_of_catalog
    )


def format_link_report(label: str, m: LinkMetrics) -> str:
    return "\n".join(
        [
            f"=== {label.upper()} — LINK ===",
            f"TP={m.true_positives} FP={m.false_positives} "
            f"FN={m.false_negatives} TN={m.true_negatives}",
            f"precision={m.precision:.3f}  recall={m.recall:.3f}  f1={m.f1:.3f}",
        ]
    )


def format_news_value_report(label: str, nv: NewsValueMetrics) -> str:
    lines = [
        f"=== {label.upper()} — NEWS-VALUE (production-news axis, 'about' rows) ===",
        f"kept-real={nv.true_positives} kept-excluded(leak)={nv.false_positives} "
        f"dropped-real={nv.false_negatives} dropped-excluded={nv.true_negatives}",
        f"precision={nv.precision:.3f}  recall={nv.recall:.3f}",
    ]
    if nv.leaks_by_category:
        leaks = ", ".join(f"{k}={v}" for k, v in sorted(nv.leaks_by_category.items()))
        lines.append(f"leaks by category: {leaks}")
    return "\n".join(lines)


def format_retrieval_report(coverage: RetrievalCoverage, tally: RetrievalTally) -> str:
    rate = "n/a" if coverage.rate is None else f"{coverage.rate:.3f}"
    mean = "n/a" if tally.mean_candidates is None else f"{tally.mean_candidates:.2f}"
    lines = [
        "=== RETRIEVAL — CANDIDATE SETS (zero-cost, scored before the model) ===",
        f"stories={tally.stories_retrieved} zero-candidate={tally.zero_candidate_stories} "
        f"cap-saturated={tally.saturated_stories} mean-candidates={mean}",
        f"expected film offered: {coverage.hits}/{coverage.total} ({rate})"
        + (
            f"  [{coverage.out_of_catalog} more expected film(s) have aged out of the active "
            "catalog and are not scored here]"
            if coverage.out_of_catalog
            else ""
        ),
    ]
    # Named, not just counted: each one is a title the scorer could not reach from its
    # headline, which is the input to tuning T and the fold rather than the prompt.
    lines += [f"  MISS  [{tmdb_id}] {title}" for title, tmdb_id in coverage.misses]
    lines.append(
        f"COVERAGE: PASS — {coverage.hits}/{coverage.total} scoreable 'about' items offered "
        "their expected film."
        if coverage.complete
        else (
            f"COVERAGE: FAIL — retrieval never offered the correct film for "
            f"{coverage.total - coverage.hits} of {coverage.total} scoreable 'about' items. "
            "That is a retrieval-stage loss, not a classifier one — check T, K and "
            "normalization before the prompt."
        )
    )
    return "\n".join(lines)


def build_stories(items: Sequence[ValidationItem]) -> list[Story]:
    """Throwaway `Story` objects carrying the fixture text. Never persisted.

    Built fresh per run: the path decides by mutating the `Story` in place, so reusing a set
    across two scorings would let the second overwrite the first's verdicts."""
    return [
        Story(id=uuid4(), source=it.source, url=it.url, title=it.title, raw={"summary": it.summary})
        for it in items
    ]


def _predicted_tmdb_ids(
    stories: Sequence[Story],
    tmdb_by_film_id: dict[UUID, int],
    *,
    unscoreable: frozenset[int] = frozenset(),
) -> list[int | None]:
    """Each story's predicted TMDB id, with `unscoreable` picks neutralized to "no link".

    A pick naming a film the catalog only learned about *after* the fixture was labeled is
    outside the label space — see `films_ingested_after`. Reading it as "no link" replays the
    counterfactual where the film was not there to pick, which scores a `none` row correct
    and an `about` row as the miss it would have been."""
    picked = [tmdb_by_film_id.get(s.film_id) if s.film_id is not None else None for s in stories]
    return [None if p in unscoreable else p for p in picked]


async def run_retrieval_path(
    *,
    client: Completer,
    model: str,
    index: CandidateIndex,
    stories: Sequence[Story],
    floor: float,
    batch_size: int,
    run_date: date,
    threshold: float,
    limit: int,
) -> tuple[list[CandidateSet], RetrievalTally]:
    """Retrieve per story, reject the zero-candidate ones without a model call, classify the
    rest against their own candidate lists. Returns each story's candidate set, in order.

    Chunked *then* split, matching `link/pipeline.py`: the zero-candidate stories drop out of
    a chunk rather than being packed around, so the classified batches here are the sizes
    production actually sends. Repacking them would measure a request shape that never ships
    — and batch size is one of the things M3 tunes (NEU-1001)."""
    candidate_sets: list[CandidateSet] = []
    tally = RetrievalTally()
    for i in range(0, len(stories), batch_size):
        batch = [
            StoryCandidates(
                story=story,
                candidates=select_candidates(
                    index,
                    headline=story.title,
                    dek=story_dek(story),
                    threshold=threshold,
                    limit=limit,
                ),
            )
            for story in stories[i : i + batch_size]
        ]
        for entry in batch:
            tally.add(entry.candidates)
            candidate_sets.append(entry.candidates)

        # A rejection is a prediction of "no film", so these stay in the scored population.
        # Dropping them would flatter retrieval by scoring it only on the stories it had
        # something to say about — on this corpus, a minority of them (ADR-0009).
        reject_zero_candidate_stories([e.story for e in batch if e.candidates.is_empty])
        await link_retrieval_story_batch(
            client=client,
            model=model,
            batch=[e for e in batch if not e.candidates.is_empty],
            floor=floor,
            run_date=run_date,
            calls=CallLog(),
        )
    return candidate_sets, tally


def _resolve_catalog_date(
    fixture_date: date | None, override: date | None
) -> tuple[date, bool, str]:
    """Which date the catalog is read as of, whether that counts as pinned, and where it came
    from — resolved once so the three don't drift apart.

    The fixture's own date wins unless overridden: a dated set scored against today's catalog
    silently loses its released subjects, which reads as recall failure rather than as drift.
    Falling back to wall clock keeps pre-pin fixtures runnable, unpinned."""
    if override is not None:
        return override, True, "--as-of override"
    if fixture_date is not None:
        return fixture_date, True, "from fixture"
    return datetime.now(UTC).date(), False, "wall clock — fixture declares no as_of_date"


def _check_coverage(expected_ids: set[int], present: set[int], label: str, *, pinned: bool) -> None:
    """Flag expected films the path cannot reach at all — a fixture/catalog mismatch that
    would otherwise read as a recall failure of the path under test.

    On a *pinned* fixture this aborts rather than warns. The whole point of the pin is that
    every labeled film was still active on the fixture's `as_of_date`, so a gap means the pin
    has drifted from the labels and the run is measuring decay again — while producing
    numbers that look entirely plausible. An unpinned fixture has no such guarantee, so there
    the gap is only worth a warning."""
    if not (missing := expected_ids - present):
        return
    message = f"{len(missing)} expected film(s) are not in the {label}: {sorted(missing)}"
    if pinned:
        raise SystemExit(
            f"ERROR: {message}\n"
            "The fixture's as_of_date does not cover every film it labels. Re-pin it to a "
            "date before the earliest of those films released, or re-label the set."
        )
    print(f"WARNING: {message}")


async def main(
    path: str,
    *,
    threshold: float,
    limit: int,
    batch_size: int,
    floor: float,
    as_of: date | None = None,
) -> None:
    settings = get_settings()
    validation_set = load_validation_set(path)
    items = validation_set.items
    catalog_date, pinned, pin_source = _resolve_catalog_date(validation_set.as_of_date, as_of)
    n_about = sum(1 for it in items if it.relation == "about")
    print(
        f"fixture: {len(items)} items total, {n_about} 'about' (linkable), "
        f"{len(items) - n_about} mention/none"
    )
    print(f"catalog read as of: {catalog_date} ({pin_source})")
    print(f"floor={floor}  model={settings.link_model}  batch_size={batch_size}")
    print(f"retrieval: threshold={threshold}  max_candidates={limit}")

    # One session for both catalog reads, so the index and the ingestion dates describe the
    # same catalog rather than one taken a few seconds later.
    async with SessionLocal() as s:
        catalog = (await s.execute(select(Film))).scalars().all()
        tmdb_by_film_id = {row.id: row.tmdb_id for row in catalog}
        ingested_at = {row.tmdb_id: row.created_at.date() for row in catalog}
        index = await build_candidate_index(s, as_of=catalog_date)

    # Picks naming films the catalog gained after labeling are not scoreable against these
    # labels (NEU-1011). Keyed off the fixture's own date, not the --as-of override: the
    # labels were written on one day and no flag moves that.
    unscoreable = films_ingested_after(validation_set.as_of_date, ingested_at)
    # A film the fixture *labels* must stay scoreable. If one were also post-labeling, its
    # correct pick would be neutralized to "no link" and score a false negative — depressing
    # recall with nothing on screen to say why. Zero on today's fixture; the pin can move.
    if stranded := {it.expected_film_tmdb_id for it in items} & unscoreable:
        raise SystemExit(
            f"ERROR: {len(stranded)} expected film(s) postdate the fixture's as_of_date: "
            f"{sorted(stranded)}\nThe labels name films the catalog gained after that date, so "
            "the pin and the labels disagree. Re-pin the fixture or re-label those rows."
        )
    if unscoreable:
        print(
            f"post-labeling films: {len(unscoreable)} of {len(ingested_at)} entered the "
            f"catalog after {validation_set.as_of_date}; picks naming them read as 'no link'"
        )

    expected_ids = {it.expected_film_tmdb_id for it in items if it.expected_film_tmdb_id}
    # The same helper the labeling scripts bound the labelable set with, so what may be
    # labeled `about` and what can be scored here cannot drift apart (spec §5.1a, §5.11).
    indexed = indexed_tmdb_ids(index.films, tmdb_by_film_id)
    _check_coverage(expected_ids, indexed, "retrieval index", pinned=pinned)

    # The prompt's own `as_of_date`, which it uses to judge whether a beat is genuinely
    # recent or re-circulated. It moves with the catalog date for the same reason: a July
    # story read as of today is stale by construction, and the model would score it
    # "downstream" on the strength of the clock rather than the copy.
    run_date = catalog_date

    stories = build_stories(items)
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        candidate_sets, tally = await run_retrieval_path(
            client=client,
            model=settings.link_model,
            index=index,
            stories=stories,
            floor=floor,
            batch_size=batch_size,
            run_date=run_date,
            threshold=threshold,
            limit=limit,
        )
    metrics = compute_link_metrics(
        link_pairs(items, _predicted_tmdb_ids(stories, tmdb_by_film_id, unscoreable=unscoreable))
    )
    nv = compute_news_value_metrics(
        news_value_rows((it, st.film_id is not None) for st, it in zip(stories, items, strict=True))
    )
    offered = [
        tuple(tmdb_by_film_id[fid] for fid in cs.film_ids if fid in tmdb_by_film_id)
        for cs in candidate_sets
    ]
    coverage = retrieval_coverage(items, offered, indexed_tmdb_ids=indexed)

    print(f"\nn={len(items)}")
    for report in (
        format_retrieval_report(coverage, tally),
        format_link_report("retrieval", metrics),
        format_news_value_report("retrieval", nv),
    ):
        print(f"\n{report}")


def _parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", default=DEFAULT_FIXTURE)
    # Overridable rather than read-only so the tuning ticket (NEU-1001) can sweep T, K and
    # batch size against this fixture without an env change and a container restart per point.
    parser.add_argument("--threshold", type=float, default=settings.link_retrieval_threshold)
    parser.add_argument("--limit", type=int, default=settings.link_retrieval_max_candidates)
    parser.add_argument("--batch-size", type=int, default=settings.link_batch_size)
    parser.add_argument("--floor", type=float, default=settings.link_confidence_floor)
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help=(
            "read the catalog as of this date instead of the fixture's own as_of_date. "
            "Counts as pinned, so coverage gaps abort and name the unreachable films — "
            "which is what you want when searching for a new pin date"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        main(
            args.fixture,
            threshold=args.threshold,
            limit=args.limit,
            batch_size=args.batch_size,
            floor=args.floor,
            as_of=args.as_of,
        )
    )
