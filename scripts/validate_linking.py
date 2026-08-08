"""Measure Stage-1 linking accuracy against the labeled fixture using the real model.

Run in the container with a real key in .env:

    task shell
    python scripts/validate_linking.py                      # both paths, one fixture
    python scripts/validate_linking.py --mode roster        # the baseline alone
    python scripts/validate_linking.py --mode retrieval --threshold 0.5 --limit 5

**Two link paths, measured in the same run.** `--mode both` (the default) sends the same
fixture down the incumbent roster path and the candidate-retrieval path and prints the
delta between them. That is cutover gate #3 — offline end-to-end F1 within ~1 point of the
roster baseline (spec §5) — and running the two together is what makes the comparison
honest: one fixture, one model, one sitting. The roster path stays until M4 deletes it
(NEU-1004), so until then it is the baseline this measures against rather than dead weight.

**Precision is reported separately, not folded into F1.** The identified risk of narrowing
the candidate set is a *precision* loss: a model shown one film and a headline about that
film's untracked sibling has nothing correct to point at, so it links the sibling's story
to the tracked film. An F1 that holds steady while precision falls and recall rises is that
regression, and a naive F1-only gate waves it through — so the gate below fails on a
precision drop past the same tolerance, and says so in as many words.

**This costs real API tokens** — `--mode both` costs roughly twice a single path, since it
classifies the fixture twice. Run it deliberately, record the numbers in the design spec,
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
    link_story_batch,
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
from upmovies.link.retrieval.index import CandidateIndex, build_candidate_index
from upmovies.link.retrieval.select import CandidateSet, select_candidates
from upmovies.link.roster import Roster, build_roster
from upmovies.link.validation import ValidationItem, load_validation_set
from upmovies.llm.client import AnthropicClient, CallLog
from upmovies.news.models import Story

DEFAULT_FIXTURE = "tests/fixtures/link/validation_set.json"

# Cutover gate #3's "~1 point". Points, not fractions: the gate is stated in points and a
# report that silently switched units would be misread against the spec.
F1_GATE_POINTS = 1.0


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


@dataclass(frozen=True)
class GateVerdict:
    """Cutover gate #3, in points, with precision judged on its own terms.

    One-sided on purpose: retrieval *beating* the baseline is not a breach. Both deltas are
    held to the same tolerance, because the failure this gate exists to catch is the one
    where only the F1 delta stays inside it."""

    precision_delta: float
    recall_delta: float
    f1_delta: float
    tolerance: float = F1_GATE_POINTS

    @property
    def f1_within_tolerance(self) -> bool:
        return self.f1_delta >= -self.tolerance

    @property
    def precision_within_tolerance(self) -> bool:
        return self.precision_delta >= -self.tolerance

    @property
    def masked_precision_loss(self) -> bool:
        """F1 passed and precision did not — the narrowing regression F1 hides (spec §4.3)."""
        return self.f1_within_tolerance and not self.precision_within_tolerance

    @property
    def passed(self) -> bool:
        return self.f1_within_tolerance and self.precision_within_tolerance


def evaluate_gate(
    baseline: LinkMetrics, candidate: LinkMetrics, *, tolerance: float = F1_GATE_POINTS
) -> GateVerdict:
    """Score the candidate path against the roster baseline, in points."""
    return GateVerdict(
        precision_delta=(candidate.precision - baseline.precision) * 100,
        recall_delta=(candidate.recall - baseline.recall) * 100,
        f1_delta=(candidate.f1 - baseline.f1) * 100,
        tolerance=tolerance,
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
    return "\n".join(lines)


def _delta_row(name: str, baseline: float, candidate: float, delta: float) -> str:
    return f"{name:<10}{baseline:>8.3f}{candidate:>11.3f}{delta:>+9.1f} pts"


def format_comparison(
    *,
    baseline: LinkMetrics,
    candidate: LinkMetrics,
    baseline_nv: NewsValueMetrics,
    candidate_nv: NewsValueMetrics,
    tolerance: float = F1_GATE_POINTS,
) -> str:
    """The roster → retrieval delta, the leak split, and the gate verdict."""
    verdict = evaluate_gate(baseline, candidate, tolerance=tolerance)
    lines = [
        f"=== ROSTER → RETRIEVAL (cutover gate #3: within {tolerance:.1f} point) ===",
        f"{'metric':<10}{'roster':>8}{'retrieval':>11}{'delta':>13}",
        _delta_row("precision", baseline.precision, candidate.precision, verdict.precision_delta),
        _delta_row("recall", baseline.recall, candidate.recall, verdict.recall_delta),
        _delta_row("f1", baseline.f1, candidate.f1, verdict.f1_delta),
    ]

    categories = sorted(set(baseline_nv.leaks_by_category) | set(candidate_nv.leaks_by_category))
    lines.append("")
    lines.append(f"not-news leaks (kept-excluded){'roster':>10}{'retrieval':>11}")
    for category in categories:
        b = baseline_nv.leaks_by_category.get(category, 0)
        c = candidate_nv.leaks_by_category.get(category, 0)
        lines.append(f"  {category:<28}{b:>9}{c:>11}")
    lines.append(
        f"  {'TOTAL':<28}{baseline_nv.false_positives:>9}{candidate_nv.false_positives:>11}"
    )

    lines.append("")
    if verdict.masked_precision_loss:
        lines.append(
            f"GATE: FAIL — F1 held ({verdict.f1_delta:+.1f} pts) while precision fell "
            f"{-verdict.precision_delta:.1f} pts and recall rose {verdict.recall_delta:+.1f}. "
            "That is the narrowing regression an F1-only gate waves through: shown one film "
            "and a headline about its untracked sibling, the model links what it was given."
        )
    elif not verdict.passed:
        lines.append(
            f"GATE: FAIL — f1 {verdict.f1_delta:+.1f} pts, precision "
            f"{verdict.precision_delta:+.1f} pts, outside the {tolerance:.1f}-point tolerance."
        )
    else:
        lines.append(
            f"GATE: PASS — f1 {verdict.f1_delta:+.1f} pts, precision "
            f"{verdict.precision_delta:+.1f} pts, both within {tolerance:.1f} point."
        )
    return "\n".join(lines)


def build_stories(items: Sequence[ValidationItem]) -> list[Story]:
    """Throwaway `Story` objects carrying the fixture text. Never persisted.

    Built fresh per path: both paths decide by mutating the `Story` in place, so a shared
    set would let the second path's verdicts overwrite the first's before they were scored."""
    return [
        Story(id=uuid4(), source=it.source, url=it.url, title=it.title, raw={"summary": it.summary})
        for it in items
    ]


def _predicted_tmdb_ids(
    stories: Sequence[Story], tmdb_by_film_id: dict[UUID, int]
) -> list[int | None]:
    return [tmdb_by_film_id.get(s.film_id) if s.film_id is not None else None for s in stories]


async def run_roster_path(
    *,
    client: Completer,
    model: str,
    roster: Roster,
    stories: Sequence[Story],
    floor: float,
    batch_size: int,
    run_date: date,
) -> None:
    """Classify every story against the whole-catalog roster prefix, in place."""
    for i in range(0, len(stories), batch_size):
        await link_story_batch(
            client=client,
            model=model,
            roster=roster,
            stories=stories[i : i + batch_size],
            floor=floor,
            run_date=run_date,
            calls=CallLog(),
        )


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
    mode: str,
    threshold: float,
    limit: int,
    batch_size: int,
    floor: float,
    tolerance: float,
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
    print(f"mode={mode}  floor={floor}  model={settings.link_model}  batch_size={batch_size}")
    if mode in ("retrieval", "both"):
        print(f"retrieval: threshold={threshold}  max_candidates={limit}")

    wants_roster = mode in ("roster", "both")
    wants_retrieval = mode in ("retrieval", "both")

    # One session for every catalog read: the roster and the index are both once-per-run
    # reads of the same active set, and reading them together is what keeps the two paths
    # measured against the same catalog rather than one taken a few seconds later.
    async with SessionLocal() as s:
        tmdb_by_film_id = dict(
            (row.id, row.tmdb_id) for row in (await s.execute(select(Film))).scalars().all()
        )
        roster = await build_roster(s, as_of=catalog_date) if wants_roster else None
        index = await build_candidate_index(s, as_of=catalog_date) if wants_retrieval else None

    expected_ids = {it.expected_film_tmdb_id for it in items if it.expected_film_tmdb_id}
    indexed_tmdb_ids: set[int] = set()
    if roster is not None:
        _check_coverage(
            expected_ids,
            {t for e in roster.entries if (t := tmdb_by_film_id.get(e.film_id)) is not None},
            "roster",
            pinned=pinned,
        )
    if index is not None:
        indexed_tmdb_ids = {
            t for f in index.films if (t := tmdb_by_film_id.get(f.film_id)) is not None
        }
        _check_coverage(expected_ids, indexed_tmdb_ids, "retrieval index", pinned=pinned)

    # The prompt's own `as_of_date`, which it uses to judge whether a beat is genuinely
    # recent or re-circulated. It moves with the catalog date for the same reason: a July
    # story read as of today is stale by construction, and the model would score it
    # "downstream" on the strength of the clock rather than the copy.
    run_date = catalog_date
    roster_metrics = retrieval_metrics = None
    roster_nv = retrieval_nv = None
    reports: list[str] = []

    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        if roster is not None:
            stories = build_stories(items)
            await run_roster_path(
                client=client,
                model=settings.link_model,
                roster=roster,
                stories=stories,
                floor=floor,
                batch_size=batch_size,
                run_date=run_date,
            )
            roster_metrics = compute_link_metrics(
                link_pairs(items, _predicted_tmdb_ids(stories, tmdb_by_film_id))
            )
            roster_nv = compute_news_value_metrics(
                news_value_rows(
                    (it, st.film_id is not None) for st, it in zip(stories, items, strict=True)
                )
            )
            reports += [
                format_link_report("roster", roster_metrics),
                format_news_value_report("roster", roster_nv),
            ]

        if index is not None:
            stories = build_stories(items)
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
            retrieval_metrics = compute_link_metrics(
                link_pairs(items, _predicted_tmdb_ids(stories, tmdb_by_film_id))
            )
            retrieval_nv = compute_news_value_metrics(
                news_value_rows(
                    (it, st.film_id is not None) for st, it in zip(stories, items, strict=True)
                )
            )
            offered = [
                tuple(tmdb_by_film_id[fid] for fid in cs.film_ids if fid in tmdb_by_film_id)
                for cs in candidate_sets
            ]
            coverage = retrieval_coverage(items, offered, indexed_tmdb_ids=indexed_tmdb_ids)
            reports += [
                format_retrieval_report(coverage, tally),
                format_link_report("retrieval", retrieval_metrics),
                format_news_value_report("retrieval", retrieval_nv),
            ]

    print(f"\nn={len(items)}")
    for report in reports:
        print(f"\n{report}")

    if (
        roster_metrics is not None
        and retrieval_metrics is not None
        and roster_nv is not None
        and retrieval_nv is not None
    ):
        print()
        print(
            format_comparison(
                baseline=roster_metrics,
                candidate=retrieval_metrics,
                baseline_nv=roster_nv,
                candidate_nv=retrieval_nv,
                tolerance=tolerance,
            )
        )


def _parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--mode",
        choices=("roster", "retrieval", "both"),
        default="both",
        help="which link path(s) to measure; 'both' scores the cutover gate (default)",
    )
    # Overridable rather than read-only so the tuning ticket (NEU-1001) can sweep T, K and
    # batch size against this fixture without an env change and a container restart per point.
    parser.add_argument("--threshold", type=float, default=settings.link_retrieval_threshold)
    parser.add_argument("--limit", type=int, default=settings.link_retrieval_max_candidates)
    parser.add_argument("--batch-size", type=int, default=settings.link_batch_size)
    parser.add_argument("--floor", type=float, default=settings.link_confidence_floor)
    # The spec states gate #3 as "~1 point" and defers the exact number to observed data
    # ("Exact numbers are confirmed from observed data before the flip", §5). The default
    # is that "~1"; the flag is what keeps the report from pinning it harder than the spec.
    parser.add_argument(
        "--tolerance",
        type=float,
        default=F1_GATE_POINTS,
        help="gate tolerance in points, applied to the f1 AND precision deltas alike",
    )
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
            mode=args.mode,
            threshold=args.threshold,
            limit=args.limit,
            batch_size=args.batch_size,
            floor=args.floor,
            tolerance=args.tolerance,
            as_of=args.as_of,
        )
    )
