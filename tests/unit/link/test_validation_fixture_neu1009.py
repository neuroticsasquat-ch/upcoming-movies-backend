from pathlib import Path

from upmovies.link.retrieval.normalize import significant_tokens
from upmovies.link.validation import load_validation_set

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "link" / "validation_set.json"

_FAST = 556682
_FAST_URL = "https://deadline.com/2026/08/f-a-s-t-release-date-warner-bros-1237016487/"

# NEU-1009: the real Deadline story that found the defect. Retrieval scored the tracked
# film at nothing at all — the headline spells it `F.A.S.T.`, the same dotted way the
# title does, so the >1-character rule dropped every letter on both sides of the index.
# The row is the recall oracle's coverage of that; removing the collapse takes the oracle
# to 93/94, red.


def test_the_dotted_initialism_row_is_in_the_gold_set():
    by_url = {it.url: it for it in load_validation_set(_FIXTURE)}

    it = by_url.get(_FAST_URL)
    assert it is not None, f"{_FAST_URL} must be in the gold set"
    assert it.expected_film_tmdb_id == _FAST
    assert it.relation == "about"
    assert it.event_type == "release_date"
    # Not an exclusion: this is production news and is expected to link.
    assert it.is_production_news is None
    assert it.exclusion_category is None


def test_the_row_exercises_the_collapse_on_both_headline_and_dek():
    """The dek re-states the initialism, so the row would still cover the collapse if the
    headline were ever re-worded — and a fix that only reached one field would not pass."""
    by_url = {it.url: it for it in load_validation_set(_FIXTURE)}
    it = by_url[_FAST_URL]

    assert "fast" in significant_tokens(it.title)
    assert "fast" in significant_tokens(it.summary)
