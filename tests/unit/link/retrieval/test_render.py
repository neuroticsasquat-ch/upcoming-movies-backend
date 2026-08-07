from uuid import uuid4

from upmovies.link.retrieval.index import indexed_film
from upmovies.link.retrieval.render import OVERVIEW_MAX, render_candidate, render_candidates
from upmovies.link.retrieval.select import CandidateSet, ScoredCandidate


def _film(**kwargs):
    base = {"film_id": uuid4(), "title": "Runner"}
    return indexed_film(**{**base, **kwargs})


def test_render_candidate_carries_the_roster_fields_plus_the_new_ones():
    film = _film(
        title="Runner",
        original_title="Corredor",
        year=2026,
        genres=["Thriller", "Drama"],
        overview="A courier runs.",
        director="Ana Vega",
        cast=["Lee Sun", "Mia Roe", "Tom Ash"],
        collection="Runner Collection",
    )
    assert render_candidate(1, film) == {
        "n": 1,
        "title": "Runner",
        "original_title": "Corredor",
        "year": 2026,
        "director": "Ana Vega",
        "cast": ["Lee Sun", "Mia Roe", "Tom Ash"],
        "collection": "Runner Collection",
        "genres": ["Thriller", "Drama"],
        "overview": "A courier runs.",
    }


def test_render_candidate_omits_empty_fields():
    rendered = render_candidate(3, _film(title="Runner"))
    assert rendered == {"n": 3, "title": "Runner"}


def test_render_candidate_omits_original_title_when_it_matches_the_title():
    rendered = render_candidate(1, _film(title="Runner", original_title="Runner"))
    assert "original_title" not in rendered


def test_render_candidate_trims_the_overview():
    film = _film(overview="x" * (OVERVIEW_MAX + 50))
    assert render_candidate(1, film)["overview"] == "x" * OVERVIEW_MAX


def test_render_candidates_numbers_from_one_in_offered_order():
    a, b = _film(title="A"), _film(title="B")
    candidate_set = CandidateSet(
        scored=(ScoredCandidate(film=a, score=1.0), ScoredCandidate(film=b, score=0.5)), limit=10
    )
    assert [c["n"] for c in render_candidates(candidate_set)] == [1, 2]
    assert [c["title"] for c in render_candidates(candidate_set)] == ["A", "B"]


def test_render_candidates_stops_at_the_cap():
    films = [_film(title=t) for t in ("A", "B", "C")]
    candidate_set = CandidateSet(
        scored=tuple(ScoredCandidate(film=f, score=1.0) for f in films), limit=2
    )
    # The cap is what the model is shown, so it is what the indices must cover — a
    # discarded film the reply could name by index would be un-resolvable.
    assert [c["title"] for c in render_candidates(candidate_set)] == ["A", "B"]


def test_render_candidates_omits_the_score():
    """Scores are retrieval-health telemetry, not evidence for the classifier. Showing
    them would invite the model to defer to the lexical ranking it is meant to check."""
    candidate_set = CandidateSet(scored=(ScoredCandidate(film=_film(), score=0.9),), limit=10)
    assert "score" not in render_candidates(candidate_set)[0]
