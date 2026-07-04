from pathlib import Path

from upmovies.link.validation import load_validation_set

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "link" / "validation_set.json"

_ANGRY_BIRDS_URLS = {
    "https://people.com/walker-scobell-and-emma-myers-play-red-s-children-in-angry-birds-3-first-look-exclusive-12005126",
    "https://tribune.com.pk/story/2614930/the-angry-birds-movie-3-reveals-walker-scobell-emma-myers-as-red-and-silvers-teenage-children",
    "https://www.aol.com/lifestyle/angry-birds-movie-3-report-123500096.html",
}


def test_angry_birds_first_look_is_gold_trailer_event():
    """NEU-445: the three first-look stories form one gold event labeled 'trailer'."""
    items = load_validation_set(_FIXTURE)
    rows = [it for it in items if it.url in _ANGRY_BIRDS_URLS]

    assert len(rows) == 3, "all three Angry Birds first-look stories must be present"
    assert all(it.relation == "about" for it in rows)
    assert all(it.event_type == "trailer" for it in rows)
    # One shared, non-empty cluster label.
    groups = {it.event_group for it in rows}
    assert len(groups) == 1 and next(iter(groups))
    # All point at the same tracked film.
    film_ids = {it.expected_film_tmdb_id for it in rows}
    assert len(film_ids) == 1 and next(iter(film_ids)) is not None
