"""The pure half of shadow observation: what one story's retrieval result says about the
roster's pick, and what a run's stories add up to."""

from uuid import UUID, uuid4

import pytest

from upmovies.link.linker import story_dek
from upmovies.link.retrieval.index import IndexedFilm, build_index, indexed_film
from upmovies.link.retrieval.select import select_candidates
from upmovies.link.shadow import RetrievalTally, observe_story
from upmovies.news.models import Story


def _film(title: str, **kwargs) -> IndexedFilm:
    return indexed_film(film_id=uuid4(), title=title, **kwargs)


def _story(title: str, *, summary: str = "", film_id: UUID | None = None, linked: bool = True):
    return Story(
        id=uuid4(),
        source="X",
        url=f"https://e/{uuid4()}",
        title=title,
        raw={"summary": summary},
        link_status="linked" if linked else "rejected",
        film_id=film_id,
    )


def _candidates(films, story: Story, **kwargs):
    # Retrieved on the same two fields the classifier reads, via the same accessor.
    return select_candidates(
        build_index(films), headline=story.title, dek=story_dek(story), **kwargs
    )


class TestObserveStory:
    def test_a_pick_retrieval_offered_first_is_recorded_at_rank_one(self):
        film = _film("Avatar: Fire and Ash")
        story = _story("Avatar Fire and Ash gets a December date", film_id=film.film_id)
        got = observe_story(story, _candidates([film], story))
        assert got is not None
        assert (got.story_id, got.film_id) == (story.id, film.film_id)
        assert (got.retrieved, got.rank, got.score, got.candidate_count) == (True, 1, 1.0, 1)

    def test_rank_is_the_picks_position_in_the_offered_set(self):
        # Two films clear the threshold; the roster picked the weaker one, which is
        # exactly the disagreement the probe exists to surface.
        strong = _film("Avatar: Fire and Ash")
        weak = _film("Avatar Rising")
        story = _story("Avatar Fire and Ash gets a date", film_id=weak.film_id)
        got = observe_story(story, _candidates([strong, weak], story))
        assert got is not None
        assert (got.retrieved, got.rank, got.candidate_count) == (True, 2, 2)

    def test_a_pick_retrieval_never_scored_is_recorded_as_a_miss(self):
        # A lexical miss: no rank and no score, which is the pairing that says K cannot
        # reach it at all (ADR-0008).
        film = _film("Nagabandham")
        story = _story("Star vehicle wraps its Hyderabad schedule", film_id=film.film_id)
        got = observe_story(story, _candidates([film], story))
        assert got is not None
        assert (got.retrieved, got.rank, got.score, got.candidate_count) == (False, None, None, 0)

    def test_a_pick_the_cap_discarded_keeps_its_score_but_has_no_rank(self):
        # Score without rank is the signal that says "raise K" rather than "the scorer
        # cannot see it" — the two failures have different fixes, so the probe keeps them
        # apart.
        films = [_film(f"Avatar Part {n}") for n in ("One", "Two", "Three")]
        story = _story("Avatar news", film_id=films[-1].film_id)
        got = observe_story(story, _candidates(films, story, threshold=0.0, limit=1))
        assert got is not None
        assert (got.retrieved, got.rank) == (False, None)
        assert got.score == pytest.approx(1 / 2)
        assert got.candidate_count == 1

    def test_a_story_the_roster_rejected_produces_no_observation(self):
        # Both paths declining is agreement, and there is nothing to adjudicate — the
        # probe table records disagreements against a pick, so a reject has no row.
        film = _film("Avatar: Fire and Ash")
        story = _story("Avatar Fire and Ash gets a date", linked=False)
        assert observe_story(story, _candidates([film], story)) is None

    def test_a_linked_story_with_no_film_produces_no_observation(self):
        # Defensive rather than reachable: `film_id` is NOT NULL on the probe because the
        # pick *is* the measurement, so a link without one has nothing to measure.
        film = _film("Avatar: Fire and Ash")
        story = _story("Avatar Fire and Ash gets a date", film_id=None)
        assert observe_story(story, _candidates([film], story)) is None

    def test_the_dek_is_retrieved_on_as_well_as_the_headline(self):
        # The live path scores headline + dek together, so shadow must too — measuring on
        # a narrower text than the one that ships would understate recall.
        film = _film("Avatar: Fire and Ash")
        story = _story("A sequel lands", summary="Avatar: Fire and Ash gets a date")
        story.film_id = film.film_id
        got = observe_story(story, _candidates([film], story))
        assert got is not None
        assert got.retrieved is True


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
