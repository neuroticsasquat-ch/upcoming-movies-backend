"""The proposer's pure half: turning one model proposal into one fixture row.

What these guard is the **pin**. The proposer shows the model the films active at the pin
and asks it to pick from that list; the fixture is then scored against the catalog as of the
same date. If the two ever disagree about which films exist, the proposer can emit an `about`
row naming a film the harness cannot reach — and `validate_linking._check_coverage` aborts the
whole run rather than quietly measuring decay (spec §5.1a). The active set is therefore the
single source of truth for what may be labeled, and `indexed_tmdb_ids` is what enforces it —
inherited unchanged from the deleted roster's `roster_tmdb_ids` (NEU-1004).
"""

from uuid import uuid4

from scripts.propose_validation_labels import _proposal_to_row
from upmovies.link.retrieval.index import IndexedFilm, indexed_film, indexed_tmdb_ids


def _entry(**kw) -> IndexedFilm:
    return indexed_film(
        film_id=kw.pop("film_id", uuid4()),
        title=kw.pop("title", "A Film"),
        year=2027,
    )


def _draft(**kw) -> dict:
    return {"url": "https://e/1", "source": "Deadline", "title": "t", "summary": "s", **kw}


def test_indexed_ids_are_the_active_films_not_the_whole_catalog():
    """The catalog holds films the active-film filter excludes — released ones, at any pin
    later than the earliest release date. Labeling one of those `about` is unscoreable: the
    link path filters it out, so the row reads as a miss for the path under test."""
    active, released = uuid4(), uuid4()
    tmdb_by_film_id = {active: 111, released: 222}

    assert indexed_tmdb_ids([_entry(film_id=active)], tmdb_by_film_id) == {111}


def test_indexed_ids_skip_entries_with_no_tmdb_id():
    entry = _entry()
    assert indexed_tmdb_ids([entry], {}) == set()


def test_an_about_proposal_naming_a_labelable_film_is_kept():
    row = _proposal_to_row(
        _draft(),
        {"relation": "about", "tmdb_id": 111, "event_type": "trailer"},
        {111},
    )

    assert row["relation"] == "about"
    assert row["expected_film_tmdb_id"] == 111
    assert row["event_type"] == "trailer"


def test_an_about_proposal_naming_an_unlabelable_film_loses_its_film_id():
    """Left for the human as an `about` row with no film — visible, and the schema refuses
    to load it, so it cannot reach the fixture unnoticed."""
    row = _proposal_to_row(
        _draft(),
        {"relation": "about", "tmdb_id": 999, "event_type": "trailer"},
        {111},
    )

    assert row["expected_film_tmdb_id"] is None


def test_a_missing_proposal_stays_todo():
    row = _proposal_to_row(_draft(), None, {111})

    assert row["relation"] == "TODO"
    assert row["expected_film_tmdb_id"] is None


def test_exclusion_category_survives_only_on_a_not_production_news_row():
    kept = _proposal_to_row(
        _draft(),
        {
            "relation": "about",
            "tmdb_id": 111,
            "event_type": "other",
            "is_production_news": False,
            "exclusion_category": "reaction",
        },
        {111},
    )
    dropped = _proposal_to_row(
        _draft(),
        {
            "relation": "about",
            "tmdb_id": 111,
            "event_type": "other",
            "is_production_news": True,
            "exclusion_category": "reaction",
        },
        {111},
    )

    assert kept["exclusion_category"] == "reaction"
    assert dropped["exclusion_category"] is None


def test_a_mention_proposal_carries_no_film_or_event():
    row = _proposal_to_row(
        _draft(), {"relation": "mention", "tmdb_id": 111, "event_type": "trailer"}, {111}
    )

    assert row["relation"] == "mention"
    assert row["expected_film_tmdb_id"] is None
    assert row["event_type"] is None
