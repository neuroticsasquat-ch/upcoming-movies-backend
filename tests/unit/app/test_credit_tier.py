"""`credit_tier`: the narrowest coverage tier that reaches one credit (D-48).

The badge `GET /people/{ref}` renders beside every credit, and the only thing standing between
it and the alert query it describes. A row labelled "Major credits" that a `major` follow does
not actually alert on is a promise the product breaks silently, which is why this is derived
from the same two predicates `_coverage_credit_clause` is built from rather than restated.
"""

import pytest

from upmovies.app.follow_queries import credit_tier


@pytest.mark.parametrize("order", [0, 1, 2])
def test_the_top_three_billed_are_lead(order):
    assert credit_tier("cast", None, order) == "lead"


def test_a_director_is_lead():
    assert credit_tier("crew", "Director", None) == "lead"


@pytest.mark.parametrize("order", [3, 4])
def test_the_rest_of_the_top_five_are_major(order):
    """`LEAD_TOP_BILLED_ORDER` is 3 and `TOP_BILLED_ORDER` is 5, deliberately and for different
    reasons — the gap between them is exactly this tier."""
    assert credit_tier("cast", None, order) == "major"


@pytest.mark.parametrize("job", ["Writer", "Screenplay"])
def test_writers_are_major(job):
    assert credit_tier("crew", job, None) == "major"


@pytest.mark.parametrize("order", [5, 11, 40])
def test_cast_billed_outside_the_top_five_is_any(order):
    assert credit_tier("cast", None, order) == "any"


def test_an_unbilled_cast_entry_is_any():
    """TMDB leaves `order` off the long tail, and NULL must read as "unbilled" rather than as
    slot 0 — the same guard `lead_credit_clause` spells in SQL."""
    assert credit_tier("cast", None, None) == "any"


@pytest.mark.parametrize("job", ["Gaffer", "Executive Producer", "Cinematographer"])
def test_a_non_seed_crew_job_is_any(job):
    assert credit_tier("crew", job, None) == "any"
