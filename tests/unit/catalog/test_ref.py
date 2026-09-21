"""`catalog/ref.py` — the `<id>-<slug>` refs the public pages resolve on.

The film pair has its own tests through `get_film_detail`'s legacy-slug path; what is proved
here is that the three id-only entities (person, studio, franchise) really do share one rule,
because they now share one implementation and a drift between them would mint three different
canonical URLs for the same scheme.
"""

import pytest

from upmovies.catalog.ref import (
    collection_ref,
    company_ref,
    parse_collection_ref,
    parse_company_ref,
    parse_person_ref,
    person_ref,
)

_MINT = (person_ref, company_ref, collection_ref)
_PARSE = (parse_person_ref, parse_company_ref, parse_collection_ref)


@pytest.mark.parametrize("mint", _MINT)
def test_a_ref_is_the_id_and_the_slugged_name(mint):
    assert mint(525, "Christopher Nolan") == "525-christopher-nolan"


@pytest.mark.parametrize("mint", _MINT)
def test_a_name_with_no_slugifiable_stem_falls_back_to_the_bare_id(mint):
    """TMDB carries names in every script, and one that transliterates to nothing is still an
    entity with a page — the bare id is a valid ref."""
    assert mint(525, "!!!") == "525"


@pytest.mark.parametrize("parse", _PARSE)
def test_a_ref_resolves_on_its_leading_id_alone(parse):
    """Everything after the first hyphen is decorative, so a stale slug still reaches the page
    and the response redirects it to the canonical spelling."""
    assert parse("525") == 525
    assert parse("525-christopher-nolan") == 525
    assert parse("525-anything-at-all") == 525


@pytest.mark.parametrize("parse", _PARSE)
def test_a_ref_that_does_not_lead_with_a_number_is_unresolvable(parse):
    assert parse("christopher-nolan") is None
    assert parse("") is None


@pytest.mark.parametrize("mint", _MINT)
@pytest.mark.parametrize("parse", _PARSE)
def test_every_mint_round_trips_through_every_parse(mint, parse):
    """One rule, spelled three ways at the call sites: a ref minted for any of the three types
    parses back to the same id under any of the three parsers."""
    assert parse(mint(263, "The Dark Knight Collection")) == 263
