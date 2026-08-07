"""The tuning sweep's arithmetic (NEU-1001).

`scripts/tune_retrieval.py` scores each story once and derives every (T, K) cell from the
recorded score vector — the shortcut that makes a grid over 98k stories affordable. These
pin that derivation, because a sweep that miscounts is worse than no sweep: it sets the
constants the live stage runs on.
"""

from uuid import uuid4

from scripts.tune_retrieval import MIN_RECORDED_SCORE, StoryScores, percentile, score_story, sweep
from upmovies.link.retrieval.index import build_index, indexed_film
from upmovies.news.models import Story


def _story(*scores: float, pick: float | None = None) -> StoryScores:
    """A story scoring `scores` (descending), optionally with the roster's pick among them."""
    ordered = tuple(sorted(scores, reverse=True))
    if pick is None:
        return StoryScores(scores=ordered)
    return StoryScores(
        scores=ordered, has_pick=True, pick_score=pick, pick_index=ordered.index(pick)
    )


class TestPercentile:
    def test_nearest_rank_never_interpolates(self):
        # p90 of ten values is the ninth, not a blend of the ninth and tenth: these are
        # candidate-set sizes, and a p90 of 4.5 films is not a thing a prompt can hold.
        assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9

    def test_the_input_order_does_not_matter(self):
        assert percentile([9, 1, 5], 0.5) == 5

    def test_an_empty_corpus_gives_zero(self):
        assert percentile([], 0.9) == 0


class TestSweep:
    def test_a_story_clearing_nothing_is_a_zero_candidate_reject(self):
        row = sweep([_story(0.4)], threshold=0.5, limit=10)
        assert row.zero_candidate == 1
        assert row.mean_offered == 0.0

    def test_the_cap_bounds_the_offered_set_and_reports_saturation(self):
        row = sweep([_story(*[1.0] * 12)], threshold=0.5, limit=10)
        assert row.saturated == 1
        assert row.mean_offered == 10.0
        assert row.over_threshold == (12,)

    def test_a_set_inside_the_cap_is_not_saturated(self):
        row = sweep([_story(1.0, 0.9)], threshold=0.5, limit=10)
        assert row.saturated == 0
        assert row.saturated_pct == 0.0

    def test_a_pick_clearing_the_threshold_inside_the_cap_is_retrieved(self):
        row = sweep([_story(1.0, 0.6, pick=0.6)], threshold=0.5, limit=10)
        assert (row.picks, row.retrieved) == (1, 1)
        assert row.recall == 1.0

    def test_a_pick_below_the_threshold_is_lost_to_T_not_to_the_cap(self):
        """The distinction the whole exercise turns on: raising K cannot recover it."""
        row = sweep([_story(1.0, 0.4, pick=0.4)], threshold=0.5, limit=10)
        assert (row.retrieved, row.lost_to_cap, row.lost_to_threshold) == (0, 0, 1)
        assert row.recall == 0.0

    def test_a_pick_pushed_past_the_cap_is_lost_to_the_cap(self):
        row = sweep([_story(*[1.0] * 10, 0.9, pick=0.9)], threshold=0.5, limit=10)
        assert (row.retrieved, row.lost_to_cap, row.lost_to_threshold) == (0, 1, 0)

    def test_an_unscored_pick_is_a_miss_at_every_threshold_including_zero(self):
        """The pick scored below `MIN_RECORDED_SCORE`, so it has no score and no rank — and
        it is still in the denominator. Recorded as a zero it would read as *retrieved* at
        T=0, which is the one place a sweep must not flatter itself."""
        unscored = StoryScores(scores=(1.0,), has_pick=True)
        for threshold in (0.0, MIN_RECORDED_SCORE, 0.5):
            row = sweep([unscored], threshold=threshold, limit=10)
            assert (row.picks, row.retrieved, row.lost_to_threshold) == (1, 0, 1)
            assert row.recall == 0.0

    def test_stories_with_no_pick_stay_out_of_the_recall_denominator(self):
        """Only stories the roster linked have a pick to adjudicate — the rest are the
        denominator for the zero-candidate rate, not for recall (spec §4.5)."""
        row = sweep([_story(1.0), _story(1.0, pick=1.0)], threshold=0.5, limit=10)
        assert (row.stories, row.picks) == (2, 1)
        assert row.recall == 1.0

    def test_recall_is_none_when_the_corpus_has_no_picks(self):
        assert sweep([_story(1.0)], threshold=0.5, limit=10).recall is None

    def test_an_empty_corpus_reports_rates_of_zero_rather_than_dividing_by_it(self):
        row = sweep([], threshold=0.5, limit=10)
        assert (row.zero_pct, row.saturated_pct, row.mean_offered) == (0.0, 0.0, 0.0)
        assert row.recall is None


class TestScoreStory:
    """What `score_story` records off one story — the input every sweep cell is derived from."""

    def _index(self, *titles: str):
        return build_index([indexed_film(film_id=uuid4(), title=title) for title in titles])

    def _news(self, headline: str, *, linked_to=None) -> Story:
        return Story(
            id=uuid4(),
            source="test",
            url=f"https://example.test/{uuid4()}",
            title=headline,
            raw={},
            link_status="linked" if linked_to else "rejected",
            film_id=linked_to,
        )

    def test_a_rejected_story_records_scores_but_no_pick(self):
        index = self._index("Avatar: Fire and Ash")
        row = score_story(index, self._news("Avatar Fire and Ash gets a date"))
        assert row.scores == (1.0,)
        assert not row.has_pick

    def test_a_linked_story_records_the_picks_score_and_position(self):
        index = self._index("Avatar: Fire and Ash", "Avatar: The Last Airbender")
        film_id = index.films[1].film_id
        row = score_story(index, self._news("Avatar Fire and Ash", linked_to=film_id))
        assert row.has_pick
        assert row.pick_index == 1
        assert row.pick_score is not None and row.pick_score < 1.0

    def test_a_pick_the_index_no_longer_holds_leaves_the_denominator(self):
        """A film released since it was linked is unreachable at any (T, K) — counting it as
        a miss would measure the calendar rather than the retriever."""
        index = self._index("Avatar: Fire and Ash")
        row = score_story(index, self._news("Avatar Fire and Ash", linked_to=uuid4()))
        assert not row.has_pick

    def test_a_pick_scoring_below_the_floor_stays_a_pick_with_no_score(self):
        # One token of six is 0.167, under `MIN_RECORDED_SCORE` — the pick is not recorded
        # among the scores, but it is still a pick, and every threshold counts it as a miss.
        index = self._index("Alpha Bravo Charlie Delta Echo Foxtrot")
        film_id = index.films[0].film_id
        row = score_story(index, self._news("Foxtrot spotted downtown", linked_to=film_id))
        assert MIN_RECORDED_SCORE == 0.2
        assert row.has_pick
        assert (row.pick_score, row.pick_index) == (None, None)
