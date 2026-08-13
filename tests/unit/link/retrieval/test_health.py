"""The per-run retrieval tally — the pure half of retrieval health, written by the shadow
observer and by the live retrieval stage alike — and the hard-breach rule read off it."""

from uuid import uuid4

import pytest

from upmovies.link.linker import story_dek
from upmovies.link.retrieval.health import (
    MAX_ZERO_CANDIDATE_RATE,
    MIN_STORIES_FOR_BREACH,
    SATURATION_WARN_RATE,
    RetrievalTally,
    hard_breach_error,
    soft_breach_note,
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
        """0.5% is what T=0.5 measures over the post-directors-tranche catalog (spec §5.13),
        down from 5.4% at NEU-1001 — zero-candidate *falls* as the catalog grows. The hard
        tier is for a collapse, not for drift; drift is the soft tier's job."""
        assert hard_breach_error(_tally(stories=1000, zero_candidate=5)) is None
        assert hard_breach_error(_tally(stories=1000, zero_candidate=1000)) is not None
        assert (MAX_ZERO_CANDIDATE_RATE, MIN_STORIES_FOR_BREACH) == (0.10, 50)

    def test_the_ceiling_still_catches_a_mis_set_threshold(self):
        """The separation test's load-bearing half (§3.5). The ceiling has to sit *below*
        what a one-step-too-high T produces or it cannot catch the failure ADR-0010 names —
        and that margin decays on its own as the catalog grows, because a mis-set T's
        zero-candidate rate falls with everything else. T=0.6 measures 18.4% on the 21-day
        grid at NEU-1135, against 25.6% at NEU-1088 and 32.6% at NEU-1001 — still clearing
        the 0.10 ceiling, but by 8.4pp where NEU-1088 had 15.6pp. The decay this ceiling was
        retuned to stop is still running; the next pass re-checks it."""
        assert hard_breach_error(_tally(stories=1000, zero_candidate=184)) is not None


def test_the_warn_rate_default_is_calibrated_at_the_chosen_k():
    """1.89% is what K=47 saturates on the 21-day grid; 5% is ~2.6x that (§3.6).

    **Reaffirmed at NEU-1135, not re-derived.** The like-for-like comparison is over one
    corpus and one catalog: 6.83% at the old K=35, 1.89% at the new K=47. NEU-1088's 1.8% is
    a different catalog *and* a different K, so it is not the other end of a measurement.

    The margin is stated against the *floor at the chosen K*, deliberately, and not against
    the spread of live readings — that spread runs to 7.89% and 7.61%, both well over 5%.
    Those are readings at a superseded K, and each is a soft breach that did its job: it
    scheduled the retune that moved K out from under it. A warn rate wide enough to cover
    them would be a warn rate that never fires."""
    assert SATURATION_WARN_RATE == 0.05
    assert soft_breach_note(RetrievalTally(stories_retrieved=1000, saturated_stories=19)) is None
    assert (
        soft_breach_note(RetrievalTally(stories_retrieved=1000, saturated_stories=200)) is not None
    )


class TestSaturationRate:
    def test_an_empty_tally_has_no_saturation_rate(self):
        # None for the same reason the zero-candidate rate is: a run with nothing pending has
        # no rate, and 0.0 would read as "the cap never bit" rather than "nothing was asked".
        assert RetrievalTally().saturation_rate is None

    def test_the_rate_is_taken_over_every_story_retrieval_ran_over(self):
        tally = RetrievalTally(stories_retrieved=200, saturated_stories=16)
        assert tally.saturation_rate == 0.08


class TestSoftBreach:
    """The soft tier (NEU-1088 §3.6). Saturation had no threshold at all, which is why it went
    0% → 7.9% in three days and nothing raised a hand — it was found by querying prod by hand.

    Warn, not hard: rising saturation is drift by definition, and it is the signal that says
    *retune*, not *outage*. `run_daily` is fail-fast, so a hard tier here would publish no
    summaries at all on a day when nothing was actually broken."""

    def test_a_rate_past_the_warn_threshold_produces_a_clause(self):
        note = soft_breach_note(
            _saturation(stories=200, saturated=40), warn_rate=0.05, min_stories=50
        )
        assert note is not None
        # Both sides of the comparison, so the run's detail line says what drifted and past what.
        assert "40" in note and "200" in note and "5" in note

    def test_a_rate_inside_the_threshold_produces_nothing(self):
        """No clause at all, rather than a reassuring one — the detail line is read at a
        glance on `/admin/runs`, and a "saturation fine" that appears on every healthy run is
        noise the eye learns to skip past."""
        assert (
            soft_breach_note(_saturation(stories=200, saturated=4), warn_rate=0.05, min_stories=50)
            is None
        )

    def test_the_threshold_itself_does_not_warn(self):
        # The highest *acceptable* rate, exactly as the hard tier reads its ceiling — which is
        # what makes 1.0 a way to switch the tier off from env rather than a permanent warning.
        assert (
            soft_breach_note(_saturation(stories=200, saturated=10), warn_rate=0.05, min_stories=50)
            is None
        )

    def test_a_small_denominator_never_warns(self):
        """The same minimum-denominator rule the hard tier carries. A tier that cries drift on
        a quiet day's four stories is one nobody reads by the time the drift is real, and this
        one exists to be believed on the day it fires."""
        assert (
            soft_breach_note(_saturation(stories=4, saturated=4), warn_rate=0.05, min_stories=50)
            is None
        )

    def test_a_run_with_nothing_pending_never_warns(self):
        assert soft_breach_note(RetrievalTally(), min_stories=0) is None

    def test_the_soft_tier_never_fails_the_run(self):
        """`soft_breach_note` returns a *detail* clause, not an error. The distinction is the
        whole decision: `hard_breach_error`'s return value is joined into the run's `error`,
        and anything that lands there finalizes the run `failed`."""
        tally = _saturation(stories=200, saturated=200)
        assert soft_breach_note(tally, warn_rate=0.05, min_stories=50) is not None
        assert hard_breach_error(tally) is None


def _saturation(*, stories: int, saturated: int) -> RetrievalTally:
    """A tally with `saturated` of `stories` truncated by the cap, built by hand.

    Zero-candidate is left at nought so the two tiers cannot be confused for one another:
    these are the counters the soft tier reads, and nothing else."""
    return RetrievalTally(stories_retrieved=stories, saturated_stories=saturated)
