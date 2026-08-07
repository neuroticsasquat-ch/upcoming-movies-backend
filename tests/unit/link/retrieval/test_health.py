"""The per-run retrieval tally — the pure half of retrieval health, written by the shadow
observer and by the live retrieval stage alike."""

from uuid import uuid4

import pytest

from upmovies.link.linker import story_dek
from upmovies.link.retrieval.health import RetrievalTally
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
