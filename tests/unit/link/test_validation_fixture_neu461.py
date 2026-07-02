from pathlib import Path

from upmovies.link.validation import load_validation_set

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "link" / "validation_set.json"
_HOUSEMAID_URL = "https://www.joblo.com/the-housemaid-trailer-new/"


def test_original_housemaid_trailer_is_no_match():
    """NEU-461: a trailer story about the ORIGINAL 'The Housemaid' must NOT link to the
    tracked sequel 'The Housemaid's Secret'. Gold label: relation 'none', no expected film."""
    items = load_validation_set(_FIXTURE)
    rows = [it for it in items if it.url == _HOUSEMAID_URL]
    assert len(rows) == 1, "the original-Housemaid trailer story must be in the gold set"
    it = rows[0]
    assert it.relation == "none"
    assert it.expected_film_tmdb_id is None
    assert "housemaid" in it.title.lower()
