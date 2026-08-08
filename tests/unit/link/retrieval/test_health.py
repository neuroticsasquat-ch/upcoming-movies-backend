"""The per-run retrieval tally — the pure half of retrieval health, written by the shadow
observer and by the live retrieval stage alike — and the hard-breach rule read off it."""

from uuid import uuid4

import pytest

from upmovies.link.linker import story_dek
from upmovies.link.retrieval.health import (
    MAX_ZERO_CANDIDATE_RATE,
    MIN_STORIES_FOR_BREACH,
    RetrievalTally,
    hard_breach_error,
)
from upmovies.link.retrieval.index import IndexedFilm, build_index, indexed_film
from upmovies.link.retrieval.select import select_candidates
from upmovies.news.models import Story


def _film(title: str, **kwargs) -> IndexedFilm:
    return indexed_film(film_id=uuid4(), title=title, **kwargs)


def _story(title: str, *, summary: str = "") -> Story:
    return Story(
        id=uuid4(), source="X", url=f"https://e/{uuid4()}", title=title, raw={"summary": summary}
    )


def _candidates(films, story: Story, **kwargs):
    # Retrieved on the same two fields the classifier reads, via the same accessor.
    return select_candidates(
        build_index(films), headline=story.title, dek=story_dek(story), **kwargs
    )


class TestRetrievalTally:
    def test_an_empty_tally_has_no_mean(self):
        # NULL rather than 0.0, which would read as "every story got zero candidates".
        tally = RetrievalTally()
        assert (tally.stories_retrieved, tally.mean_candidates) == (0, None)

    def test_every_story_counts_toward_the_denominator(self):
        # Including the stories the probe table never sees: the zero-candidate majority
        # is precisely what the denominator is for.
        film = _film("Avatar: Fire and Ash")
        tally = RetrievalTally()
        for headline in ("Avatar Fire and Ash dated", "Unrelated TV news", "More TV news"):
            tally.add(_candidates([film], _story(headline)))
        assert (tally.stories_retrieved, tally.zero_candidate_stories) == (3, 2)

    def test_the_mean_is_taken_over_the_offered_sets(self):
        # Post-cap, so it reads as prompt size rather than match volume.
        films = [_film(f"Avatar Part {n}") for n in ("One", "Two", "Three")]
        tally = RetrievalTally()
        tally.add(_candidates(films, _story("Avatar news"), threshold=0.0, limit=2))
        tally.add(_candidates(films, _story("Unrelated TV news")))
        assert tally.mean_candidates == pytest.approx(1.0)

    def test_saturation_counts_the_stories_the_cap_bit(self):
        films = [_film(f"Avatar Part {n}") for n in ("One", "Two", "Three")]
        tally = RetrievalTally()
        tally.add(_candidates(films, _story("Avatar news"), threshold=0.0, limit=2))
        tally.add(_candidates(films, _story("Avatar news"), threshold=0.0, limit=3))
        assert (tally.stories_retrieved, tally.saturated_stories) == (2, 1)

    def test_an_empty_tally_has_no_zero_candidate_rate(self):
        # Same reasoning as `mean_candidates`: with no denominator a 0.0 would read as a
        # perfectly healthy run, and a 1.0 as a total one. Neither is knowable.
        assert RetrievalTally().zero_candidate_rate is None

    def test_the_zero_candidate_rate_is_taken_over_every_story(self):
        film = _film("Avatar: Fire and Ash")
        tally = RetrievalTally()
        for headline in ("Avatar Fire and Ash dated", "Unrelated TV news", "More TV news"):
            tally.add(_candidates([film], _story(headline)))
        assert tally.zero_candidate_rate == pytest.approx(2 / 3)


def _tally(*, stories: int, zero_candidate: int) -> RetrievalTally:
    """A tally with the two counters the breach rule reads, set directly.

    Built by hand rather than by retrieving: the rule is a pure function of the counters,
    and constructing a corpus that happens to produce a given rate would test the scorer."""
    return RetrievalTally(stories_retrieved=stories, zero_candidate_stories=zero_candidate)


class TestHardBreach:
    def test_a_rate_past_the_threshold_is_a_breach(self):
        error = hard_breach_error(
            _tally(stories=100, zero_candidate=60), max_zero_candidate_rate=0.25, min_stories=50
        )
        assert error is not None
        # Both sides of the comparison, so the run row says what tripped and against what.
        assert "60" in error and "100" in error and "25" in error

    def test_a_healthy_rate_is_not(self):
        assert (
            hard_breach_error(
                _tally(stories=100, zero_candidate=5), max_zero_candidate_rate=0.25, min_stories=50
            )
            is None
        )

    def test_the_threshold_itself_is_not_a_breach(self):
        # `max_zero_candidate_rate` is the highest *acceptable* rate, which is what makes 1.0
        # a way to switch the guard off rather than a guard that fires on every run.
        assert (
            hard_breach_error(
                _tally(stories=100, zero_candidate=25), max_zero_candidate_rate=0.25, min_stories=50
            )
            is None
        )

    def test_a_small_denominator_never_breaches(self):
        """The minimum-denominator rule, mirroring `total_failure_error`'s refusal to let a
        thin backlog fail the daily chain. `run_daily` is fail-fast, so a quiet day whose
        four stories happen to miss must not stop the summaries from publishing."""
        assert (
            hard_breach_error(
                _tally(stories=4, zero_candidate=4), max_zero_candidate_rate=0.25, min_stories=50
            )
            is None
        )

    def test_the_minimum_denominator_is_inclusive(self):
        assert (
            hard_breach_error(
                _tally(stories=50, zero_candidate=50), max_zero_candidate_rate=0.25, min_stories=50
            )
            is not None
        )

    def test_a_run_with_nothing_pending_never_breaches(self):
        assert hard_breach_error(RetrievalTally(), min_stories=0) is None

    def test_the_defaults_leave_the_measured_rate_ample_room(self):
        """5.4% is what T=0.5/K=25 measured over 98,662 production stories (spec §5.2), and
        the growth curve says it falls further as the catalog expands. The hard tier is for a
        collapse, not for drift — drift is the soft tier's job, on `/admin/runs`."""
        assert hard_breach_error(_tally(stories=1000, zero_candidate=54)) is None
        assert hard_breach_error(_tally(stories=1000, zero_candidate=1000)) is not None
        assert (MAX_ZERO_CANDIDATE_RATE, MIN_STORIES_FOR_BREACH) == (0.25, 50)
