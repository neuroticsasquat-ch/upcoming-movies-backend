import json
from datetime import date
from uuid import uuid4

from scripts.validate_linking import (
    RetrievalCoverage,
    _predicted_tmdb_ids,
    build_stories,
    format_retrieval_report,
    link_pairs,
    retrieval_coverage,
    run_retrieval_path,
)
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


def test_coverage_is_complete_only_when_every_scoreable_about_item_was_offered():
    """Gate #3a, the one check of the three that survived the roster's deletion (NEU-1004):
    3b and 3c were deltas against the roster baseline and have no baseline any more."""
    assert RetrievalCoverage(hits=94, total=94, misses=()).complete
    assert not RetrievalCoverage(hits=534, total=538, misses=()).complete


def test_retrieval_report_passes_coverage_when_every_expected_film_was_offered():
    report = format_retrieval_report(
        RetrievalCoverage(hits=2, total=2, misses=()), RetrievalTally(stories_retrieved=2)
    )
    assert "COVERAGE: PASS" in report
    assert "2/2" in report


def test_retrieval_report_fails_coverage_and_points_at_the_retrieval_stage():
    """A coverage gap is a lexical failure with a lexical fix — the report has to say so, or
    the next person tunes the prompt (spec §5.11)."""
    report = format_retrieval_report(
        RetrievalCoverage(hits=1, total=2, misses=(("Naga Bandham", 9),)),
        RetrievalTally(stories_retrieved=2),
    )
    assert "COVERAGE: FAIL" in report
    assert "1 of 2" in report
    assert "check T, K and normalization" in report


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
