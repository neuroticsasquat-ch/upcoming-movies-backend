from uuid import uuid4

import pytest

from upmovies.link.retrieval.index import IndexedFilm, build_index, indexed_film
from upmovies.link.retrieval.normalize import squash_fold
from upmovies.link.retrieval.select import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_SCORE_THRESHOLD,
    CandidateSet,
    select_candidates,
)


def _film(title: str, **kwargs) -> IndexedFilm:
    return indexed_film(film_id=uuid4(), title=title, **kwargs)


def _select(films, headline: str, dek: str | None = None, **kwargs) -> CandidateSet:
    return select_candidates(build_index(films), headline=headline, dek=dek, **kwargs)


class TestScoring:
    def test_every_title_token_present_scores_one(self):
        film = _film("Avatar: Fire and Ash")
        got = _select([film], "Avatar Fire and Ash gets a December date")
        assert [c.score for c in got.candidates] == [1.0]

    def test_score_is_the_fraction_of_title_tokens_present(self):
        # "avatar" and "fire" of {avatar, fire, ash} — "and" is a stopword and never
        # enters the title's token count.
        film = _film("Avatar: Fire and Ash")
        got = _select([film], "Avatar Fire sequel news", threshold=0.0)
        assert [c.score for c in got.candidates] == [2 / 3]

    def test_a_repeated_title_token_is_counted_once(self):
        # Scoring over distinct tokens: a title that says "spider" twice is not twice as
        # hard to match, and the list-based denominator would make it so.
        film = _film("Spider-Man: Spider Verse")
        got = _select([film], "Spider news", threshold=0.0)
        assert [c.score for c in got.candidates] == [1 / 3]

    def test_the_dek_counts_as_story_text(self):
        film = _film("Avatar: Fire and Ash")
        got = _select([film], "A sequel lands", dek="Avatar: Fire and Ash gets a date")
        assert [c.score for c in got.candidates] == [1.0]

    def test_a_missing_dek_is_allowed(self):
        film = _film("Avatar: Fire and Ash")
        got = _select([film], "Avatar Fire and Ash gets a date", dek=None)
        assert [c.score for c in got.candidates] == [1.0]

    def test_the_best_title_wins(self):
        # The original title matches in full where the primary title does not; the film
        # is scored on its best route in, not its first.
        film = _film("The Runner", original_title="Coureur Solitaire")
        got = _select([film], "Coureur Solitaire wraps shooting", threshold=0.0)
        assert [c.score for c in got.candidates] == [1.0]

    def test_an_alternative_title_can_be_the_best_route(self):
        film = _film("The Runner", alternative_titles=("El Corredor",))
        got = _select([film], "El Corredor wraps shooting", threshold=0.0)
        assert [c.score for c in got.candidates] == [1.0]

    def test_a_title_with_no_significant_tokens_does_not_score_full_credit(self):
        # "The A of" tokenizes to nothing. 0/0 is not 1.0 — a title that says nothing
        # must not match every story. The film is reachable through its real title, so
        # a missing guard would surface here as a 1.0 instead of the 1/3 it earned.
        film = _film("Avatar: Fire and Ash", alternative_titles=("The A of",))
        got = _select([film], "Avatar news", threshold=0.0)
        assert [c.score for c in got.candidates] == [1 / 3]


class TestSquashFoldRescue:
    def test_a_folded_title_matching_as_a_substring_scores_full_credit(self):
        # The corpus's one genuine miss: tracked "Nagabandham" shares no token with
        # "Naga Bandham Movie Trailer Launch", so only the fold can reach it.
        film = _film("Nagabandham")
        got = _select([film], "Naga Bandham Movie Trailer Launch")
        assert [c.score for c in got.candidates] == [1.0]

    def test_the_rescue_reads_the_dek_too(self):
        film = _film("Nagabandham")
        got = _select([film], "Trailer launched", dek="A first look at Naga Bandham")
        assert [c.score for c in got.candidates] == [1.0]

    def test_a_short_fold_never_rescues(self):
        # "Sunup" folds to five characters, below SQUASH_FOLD_MIN_CHARS. It *is* a
        # substring of the folded story, and matching on it is exactly the false
        # positive the guard exists to prevent — the story is about something else.
        film = _film("Sunup")
        assert squash_fold("Sunup") in squash_fold("Sunupdate on the schedule")
        got = _select([film], "Sunupdate on the schedule", threshold=0.0)
        assert got.candidates == ()

    def test_the_rescue_never_matches_across_the_headline_dek_seam(self):
        # squash_fold strips whitespace, so folding "headline + dek" as one string fuses
        # the headline's last word onto the dek's first. "…shot at night" beside
        # "Watchmakers assemble…" would fold to "…nightwatchmakers…" and rescue
        # "Nightwatch" at full credit for a story neither field is about.
        film = _film("Nightwatch")
        got = _select(
            [film],
            "The scene was shot at night",
            dek="Watchmakers assemble for the sequel",
            threshold=0.0,
        )
        assert got.candidates == ()

    def test_the_rescue_beats_a_partial_token_score(self):
        film = _film("Naga Bandham")
        got = _select([film], "Nagabandham teaser and Naga news", threshold=0.0)
        assert [c.score for c in got.candidates] == [1.0]


class TestThreshold:
    def test_films_scoring_below_the_threshold_are_excluded(self):
        film = _film("Avatar: Fire and Ash")
        assert _select([film], "Avatar news", threshold=0.5).candidates == ()

    def test_a_film_scoring_exactly_the_threshold_is_included(self):
        film = _film("Avatar: Fire and Ash")
        got = _select([film], "Avatar news", threshold=1 / 3)
        assert [c.score for c in got.candidates] == [1 / 3]

    def test_a_story_matching_nothing_yields_an_empty_set(self):
        # A legitimate outcome, not an error: it is a zero-candidate rejection
        # downstream (ADR-0009).
        got = _select([_film("Avatar: Fire and Ash")], "Local election results")
        assert got.candidates == ()
        assert got.is_empty

    def test_the_default_threshold_is_the_value_tuned_against_production_traffic(self):
        # NEU-1001: every roster pick retrieval can reach scores 0.5 or better, so 0.5 is
        # the top of the flat region — 0.6 costs 11 of 365 picks.
        assert DEFAULT_SCORE_THRESHOLD == 0.5


class TestCapAndOrdering:
    def test_candidates_are_ordered_by_descending_score(self):
        exact = _film("Avatar: Fire and Ash")
        partial = _film("Avatar: The Last Airbender")
        got = _select([partial, exact], "Avatar Fire and Ash gets a date", threshold=0.0)
        assert [c.film.film_id for c in got.candidates] == [exact.film_id, partial.film_id]
        assert got.candidates[0].score > got.candidates[1].score

    def test_ties_are_broken_by_title_so_the_order_is_deterministic(self):
        b = _film("Avatar: Bbb")
        a = _film("Avatar: Aaa")
        got = _select([b, a], "Avatar news", threshold=0.0)
        assert [c.film.title for c in got.candidates] == ["Avatar: Aaa", "Avatar: Bbb"]

    def test_the_cap_keeps_the_best_scoring_films(self):
        exact = _film("Avatar: Fire and Ash")
        others = [_film(f"Avatar: Story {i}") for i in range(5)]
        got = _select([*others, exact], "Avatar Fire and Ash", threshold=0.0, limit=1)
        assert [c.film.film_id for c in got.candidates] == [exact.film_id]

    def test_the_cap_is_reported_as_saturation_when_it_discards_a_film(self):
        films = [_film(f"Avatar: Story {i}") for i in range(4)]
        got = _select(films, "Avatar news", threshold=0.0, limit=2)
        assert len(got.candidates) == 2
        assert got.over_threshold == 4
        assert got.saturated

    def test_a_set_inside_the_cap_is_not_saturated(self):
        films = [_film(f"Avatar: Story {i}") for i in range(2)]
        got = _select(films, "Avatar news", threshold=0.0, limit=2)
        assert got.over_threshold == 2
        assert not got.saturated

    def test_the_default_cap_is_the_value_tuned_against_production_traffic(self):
        # NEU-1001: p99 of the over-threshold set is 18 films and the deepest roster pick
        # sits at rank 21, so 25 caps the tail without discarding a pick.
        assert DEFAULT_CANDIDATE_LIMIT == 25


class TestTelemetryInterface:
    def test_rank_and_score_of_a_candidate_are_retrievable(self):
        exact = _film("Avatar: Fire and Ash")
        partial = _film("Avatar: The Last Airbender")
        got = _select([exact, partial], "Avatar Fire and Ash gets a date", threshold=0.0)
        assert got.rank_of(exact.film_id) == 1
        assert got.rank_of(partial.film_id) == 2
        assert got.score_of(exact.film_id) == 1.0

    def test_a_film_that_was_not_retrieved_has_no_rank_or_score(self):
        film = _film("Avatar: Fire and Ash")
        missed = _film("Runner")
        got = _select([film, missed], "Avatar Fire and Ash gets a date")
        assert got.rank_of(missed.film_id) is None
        assert got.score_of(missed.film_id) is None

    def test_a_film_discarded_by_the_cap_has_no_rank_but_keeps_its_score(self):
        # The pairing shadow telemetry reads: a score with no rank means the roster's
        # pick was retrieved and then lost to the cap, which is a different failure from
        # a lexical miss and has a different fix. Collapsing both to None would make the
        # design's "misses are score-zero misses" claim unverifiable in shadow.
        exact = _film("Avatar: Fire and Ash")
        partial = _film("Avatar: The Last Airbender")
        got = _select([exact, partial], "Avatar Fire and Ash", threshold=0.0, limit=1)
        assert got.rank_of(partial.film_id) is None
        assert got.score_of(partial.film_id) == 1 / 3
        assert got.saturated

    def test_a_film_that_never_cleared_the_threshold_has_neither_rank_nor_score(self):
        exact = _film("Avatar: Fire and Ash")
        below = _film("Avatar: The Last Airbender")
        got = _select([exact, below], "Avatar Fire and Ash", threshold=0.5)
        assert got.rank_of(below.film_id) is None
        assert got.score_of(below.film_id) is None

    def test_film_ids_exposes_the_offered_set(self):
        exact = _film("Avatar: Fire and Ash")
        got = _select([exact], "Avatar Fire and Ash gets a date")
        assert got.film_ids == (exact.film_id,)

    def test_candidates_carry_the_display_fields_for_rendering(self):
        film = _film("Runner", year=2027, director="Ana Ruiz", cast=("Lee Park",))
        got = _select([film], "Runner wraps shooting")
        (candidate,) = got.candidates
        assert (candidate.film.year, candidate.film.director) == (2027, "Ana Ruiz")
        assert candidate.film.cast == ("Lee Park",)


class TestFilmIdForIndex:
    """Resolving the model's reply back to a film — 1-based, local to this story's set."""

    def test_it_resolves_a_one_based_index_into_the_offered_set(self):
        first, second = _film("Avatar Part One"), _film("Avatar Part Two")
        got = _select([first, second], "Avatar Part One and Avatar Part Two", threshold=0.0)
        offered = got.film_ids
        assert got.film_id_for_index(1) == offered[0]
        assert got.film_id_for_index(2) == offered[1]

    def test_an_index_past_the_offered_set_names_no_film(self):
        got = _select([_film("Avatar: Fire and Ash")], "Avatar Fire and Ash gets a date")
        assert got.film_id_for_index(2) is None

    def test_a_film_the_cap_discarded_is_not_nameable(self):
        # It was never shown, so the model cannot have meant it — and coercing an index
        # onto a hidden film would be a link nobody asked for.
        films = [_film(f"Avatar Part {n}") for n in ("One", "Two", "Three")]
        got = _select(films, "Avatar Part One Two Three", threshold=0.0, limit=1)
        assert got.over_threshold == 3
        assert got.film_id_for_index(2) is None

    @pytest.mark.parametrize("index", [0, -1, None, "1", 1.0, True])
    def test_anything_that_is_not_a_positive_int_names_no_film(self, index):
        # `True` is an `int` in Python and would otherwise resolve to the first candidate.
        got = _select([_film("Avatar: Fire and Ash")], "Avatar Fire and Ash gets a date")
        assert got.film_id_for_index(index) is None

    def test_an_empty_set_names_no_film_at_any_index(self):
        assert CandidateSet(scored=(), limit=10).film_id_for_index(1) is None


class TestEmptyInputs:
    def test_an_empty_index_yields_an_empty_set(self):
        got = _select([], "Avatar Fire and Ash gets a date")
        assert got.is_empty
        assert got.over_threshold == 0
        assert not got.saturated

    def test_a_headline_with_no_significant_tokens_yields_an_empty_set(self):
        assert _select([_film("Avatar: Fire and Ash")], "A of the").is_empty
