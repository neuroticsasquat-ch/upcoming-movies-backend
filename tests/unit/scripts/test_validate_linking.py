import json
from datetime import date
from uuid import uuid4

from scripts.validate_linking import (
    RetrievalCoverage,
    _predicted_tmdb_ids,
    build_stories,
    evaluate_gate,
    format_comparison,
    format_retrieval_report,
    link_pairs,
    retrieval_coverage,
    run_retrieval_path,
)
from upmovies.link.metrics import LinkMetrics, compute_link_metrics, compute_news_value_metrics
from upmovies.link.retrieval.health import RetrievalTally
from upmovies.link.retrieval.index import build_index, indexed_film
from upmovies.link.validation import ValidationItem
from upmovies.llm.client import CallResult
from upmovies.news.models import Story


def _about(tmdb_id: int, title: str = "t", **kw) -> ValidationItem:
    base = dict(
        url=f"u{tmdb_id}",
        source="s",
        title=title,
        summary="",
        relation="about",
        expected_film_tmdb_id=tmdb_id,
    )
    base.update(kw)
    return ValidationItem.model_validate(base)


def _none(url: str = "u0") -> ValidationItem:
    return ValidationItem.model_validate(
        dict(url=url, source="s", title="t", summary="", relation="none")
    )


def _counts(tp: int, fp: int, *, about: int = 94) -> LinkMetrics:
    """Metrics from whole-item counts, which is what the restated gate scores on.

    `about` defaults to the fixture's own 94 scoreable items so the numbers in these tests
    read against spec §5.8 directly."""
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / about
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return LinkMetrics(tp, fp, about - tp, 0, precision, recall, f1)


def _coverage(hits: int, total: int) -> RetrievalCoverage:
    return RetrievalCoverage(hits=hits, total=total, misses=(), out_of_catalog=0)


def test_link_pairs_zips_predictions_onto_expected_ids():
    items = [_about(7), _none(), _about(9)]
    assert link_pairs(items, [7, None, 11]) == [(7, 7), (None, None), (11, 9)]


def test_retrieval_coverage_scores_about_items_only():
    items = [_about(7, "Hit"), _about(9, "Miss"), _none()]
    coverage = retrieval_coverage(items, [(7, 22), (22,), ()])
    assert (coverage.hits, coverage.total) == (1, 2)
    assert coverage.misses == (("Miss", 9),)
    assert coverage.rate == 0.5


def test_retrieval_coverage_has_no_rate_without_about_items():
    assert retrieval_coverage([_none()], [()]).rate is None


def test_retrieval_coverage_sets_aside_films_that_left_the_active_catalog():
    """The fixture outlives the release dates of the films it names. A film retrieval was
    never allowed to consider is a scope loss, not a lexical one, and scoring it as a miss
    would invite the fix — a lower T, another alias — that could not have helped."""
    items = [_about(7, "Hit"), _about(9, "Released last month")]
    coverage = retrieval_coverage(items, [(7,), ()], indexed_tmdb_ids={7})
    assert (coverage.hits, coverage.total, coverage.out_of_catalog) == (1, 1, 1)
    assert coverage.misses == ()
    assert coverage.rate == 1.0


def test_retrieval_report_names_misses_and_flags_the_aged_out_films():
    coverage = RetrievalCoverage(hits=1, total=2, misses=(("Naga Bandham", 9),), out_of_catalog=3)
    tally = RetrievalTally(
        stories_retrieved=4, zero_candidate_stories=2, saturated_stories=1, candidates_offered=6
    )
    report = format_retrieval_report(coverage, tally)
    assert "zero-candidate=2" in report and "cap-saturated=1" in report
    assert "mean-candidates=1.50" in report
    assert "MISS  [9] Naga Bandham" in report
    assert "3 more expected film(s) have aged out" in report


def test_gate_passes_at_the_shipped_configuration():
    """Spec §5.7 run D, the numbers ADR-0011 accepts: retrieval keeps every link the roster
    made and adds exactly the ceiling's worth of false positives."""
    verdict = evaluate_gate(_counts(67, 2), _counts(68, 5), coverage=_coverage(94, 94))
    assert verdict.passed
    # One link gained, three wrong ones bought with it — the trade ADR-0011 accepts.
    assert (verdict.excess_false_positives, verdict.lost_true_positives) == (3, -1)
    assert verdict.at_the_boundary
    assert not verdict.narrowing_regression


def test_gate_passes_when_retrieval_beats_the_baseline():
    """A gain is not a breach — gate #3 is one-sided, and stays one-sided in items."""
    assert evaluate_gate(_counts(60, 8), _counts(70, 2), coverage=_coverage(94, 94)).passed


def test_gate_fails_when_retrieval_adds_false_positives_past_the_ceiling():
    verdict = evaluate_gate(_counts(67, 2), _counts(68, 6), coverage=_coverage(94, 94))
    assert not verdict.passed
    assert verdict.excess_false_positives == 4
    assert not verdict.at_the_boundary


def test_gate_fails_when_retrieval_loses_a_link_the_roster_made():
    """3b. Losing links is the failure the item gate must not let a precision gain mask —
    `link` is lossy, so a link not made today is never made."""
    verdict = evaluate_gate(_counts(67, 2), _counts(66, 0), coverage=_coverage(94, 94))
    assert not verdict.passed
    assert verdict.lost_true_positives == 1


def test_gate_fails_when_retrieval_never_offered_the_right_film():
    """3a. The only part of gate #3 that is actually about the retrieval stage."""
    verdict = evaluate_gate(_counts(67, 2), _counts(68, 5), coverage=_coverage(93, 94))
    assert not verdict.passed
    assert verdict.coverage_complete is False


def test_gate_names_the_narrowing_regression_it_was_written_to_catch():
    """Spec §4.3's failure, in items: retrieval links at least as much as the roster while
    linking more stories it should not have. The points form of this test asserted a steady
    F1 masking a precision fall; the item form is the same event, one resolution coarser."""
    verdict = evaluate_gate(_counts(68, 12), _counts(70, 20), coverage=_coverage(94, 94))
    assert verdict.narrowing_regression
    assert not verdict.passed


def test_gate_leaves_coverage_unscored_when_it_was_not_measured():
    """`--mode retrieval` has no baseline and never reaches the gate; a verdict built without
    coverage must say so rather than silently credit 3a."""
    verdict = evaluate_gate(_counts(67, 2), _counts(68, 5))
    assert verdict.coverage_complete is None


def test_comparison_report_keeps_the_points_deltas_and_prints_the_item_verdict():
    """ADR-0011: the points deltas stay in the report because §5.1a and §5.7 are written in
    them, but they stop being the pass condition."""
    nv = compute_news_value_metrics([(True, False, "reaction")])
    report = format_comparison(
        baseline=_counts(67, 2),
        candidate=_counts(68, 5),
        baseline_nv=nv,
        candidate_nv=nv,
        coverage=_coverage(94, 94),
    )
    assert "precision" in report and "recall" in report and "f1" in report
    assert "false positives" in report and "candidate coverage" in report
    assert "GATE: PASS" in report
    assert "reaction" in report  # leaks broken out by category, both paths side by side


def test_comparison_report_still_computes_metrics_from_scored_pairs():
    """The scorer feeding the gate is unchanged — only what the gate does with it moved."""
    baseline = compute_link_metrics([(1, 1), (2, 2), (3, None)])
    candidate = compute_link_metrics([(1, 1), (2, 2), (3, None), (4, None)])
    nv = compute_news_value_metrics([(True, False, "reaction")])
    report = format_comparison(
        baseline=baseline, candidate=candidate, baseline_nv=nv, candidate_nv=nv
    )
    assert "GATE" in report


class _LinkFirstCandidate:
    """A classifier that links every story it is asked about to its first candidate."""

    def __init__(self):
        self.seen_story_ids: list[str] = []

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        stories = json.loads(messages[0]["content"])["stories"]
        self.seen_story_ids += [s["id"] for s in stories]
        return calls.record(
            CallResult(
                text=json.dumps(
                    [
                        {"id": s["id"], "film": 1, "confidence": 0.95, "reason": "about"}
                        for s in stories
                    ]
                )
            )
        )


async def test_retrieval_path_rejects_zero_candidate_stories_without_a_model_call():
    """The measured population is every fixture item, not just the ones retrieval reached —
    a zero-candidate story is a prediction of 'no film' and must be scored as one."""
    film_id = uuid4()
    index = build_index([indexed_film(film_id=film_id, title="Nagabandham")])
    items = [_about(7, "Naga Bandham Movie Trailer Launch"), _about(9, "Unrelated sports result")]
    stories = build_stories(items)
    client = _LinkFirstCandidate()

    candidate_sets, tally = await run_retrieval_path(
        client=client,
        model="m",
        index=index,
        stories=stories,
        floor=0.7,
        batch_size=15,
        run_date=date(2026, 8, 7),
        threshold=0.34,
        limit=10,
    )

    assert client.seen_story_ids == [str(stories[0].id)]
    assert stories[0].link_status == "linked" and stories[0].film_id == film_id
    assert stories[1].link_status == "rejected" and stories[1].link_note == "no-candidates"
    assert [cs.is_empty for cs in candidate_sets] == [False, True]
    assert (tally.stories_retrieved, tally.zero_candidate_stories) == (2, 1)


async def test_retrieval_path_sends_no_call_for_an_all_zero_candidate_batch():
    index = build_index([indexed_film(film_id=uuid4(), title="Nagabandham")])
    stories = build_stories([_about(9, "Unrelated sports result")])
    client = _LinkFirstCandidate()

    await run_retrieval_path(
        client=client,
        model="m",
        index=index,
        stories=stories,
        floor=0.7,
        batch_size=15,
        run_date=date(2026, 8, 7),
        threshold=0.34,
        limit=10,
    )

    assert client.seen_story_ids == []


def test_predicted_tmdb_ids_neutralizes_post_labeling_picks():
    """A pick naming a film the catalog gained after labeling reads as "no link" — the
    counterfactual where it was not there to pick. This is where the exclusion actually
    reaches the metrics, so a `none` row scores a true negative rather than a false
    positive (NEU-1011)."""
    film_a, film_b = uuid4(), uuid4()
    stories = [
        Story(id=uuid4(), source="s", url="u1", title="t1", film_id=film_a),
        Story(id=uuid4(), source="s", url="u2", title="t2", film_id=film_b),
        Story(id=uuid4(), source="s", url="u3", title="t3", film_id=None),
    ]
    tmdb_by_film_id = {film_a: 11, film_b: 22}

    assert _predicted_tmdb_ids(stories, tmdb_by_film_id) == [11, 22, None]
    assert _predicted_tmdb_ids(stories, tmdb_by_film_id, unscoreable=frozenset({22})) == [
        11,
        None,
        None,
    ]


def test_neutralized_pick_scores_a_none_row_as_a_true_negative():
    """The point of the neutralization, end to end: the link was real but unlabelable, so it
    must not land as a false positive against a `none` label."""
    items = [ValidationItem(url="u", source="s", title="t", relation="none")]
    film_id = uuid4()
    stories = [Story(id=uuid4(), source="s", url="u", title="t", film_id=film_id)]
    tmdb_by_film_id = {film_id: 99}

    scored = link_pairs(
        items, _predicted_tmdb_ids(stories, tmdb_by_film_id, unscoreable=frozenset({99}))
    )

    assert scored == [(None, None)]  # a correct decline, not a false positive
