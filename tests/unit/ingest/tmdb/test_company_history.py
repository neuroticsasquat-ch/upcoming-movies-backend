"""The company diff in isolation (EF-5, NEU-1433).

The rule under test is the one the whole studio half rests on: first observation is a
baseline, never a change. It is a property of `diff_companies` alone, which is why it is
pinned here as well as through a real `upsert_film` round trip.
"""

from tests.fixtures.tmdb import make_details
from upmovies.ingest.tmdb.company_history import (
    COMPANY_ADDED,
    COMPANY_REMOVED,
    CompanyChange,
    companies_from_details,
    diff_companies,
)
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails


def _details(tmdb_id: int, company_ids: list[int]) -> TMDBMovieDetails:
    return TMDBMovieDetails.model_validate(
        make_details(
            tmdb_id,
            production_companies=[
                {"id": company_id, "name": f"Studio {company_id}"} for company_id in company_ids
            ],
        )
    )


def test_first_observation_is_a_baseline_however_many_companies_arrive():
    """`previous=None` is the film's first observed company set. Whatever it contains, it is
    baseline — the rule that stops admitting the catalog writing a false attachment per
    company per film."""
    assert diff_companies(previous=None, current={1, 2, 3}) == []


def test_an_observed_film_holding_nothing_is_not_a_baseline():
    """`previous=set()` is a different statement from `previous=None`: the film *was* observed
    and TMDB listed no companies. The studio that arrives next is a genuine attachment."""
    assert diff_companies(previous=set(), current={7}) == [
        CompanyChange(company_id=7, change=COMPANY_ADDED)
    ]


def test_an_unchanged_set_is_no_change():
    assert diff_companies(previous={1, 2}, current={2, 1}) == []


def test_additions_come_before_removals_each_sorted():
    changes = diff_companies(previous={1, 2}, current={2, 5, 4})
    assert changes == [
        CompanyChange(company_id=4, change=COMPANY_ADDED),
        CompanyChange(company_id=5, change=COMPANY_ADDED),
        CompanyChange(company_id=1, change=COMPANY_REMOVED),
    ]


def test_every_company_leaving_is_recorded():
    assert diff_companies(previous={1, 2}, current=set()) == [
        CompanyChange(company_id=1, change=COMPANY_REMOVED),
        CompanyChange(company_id=2, change=COMPANY_REMOVED),
    ]


def test_companies_from_details_reads_the_payload_ids():
    assert companies_from_details(_details(1, [20, 4])) == {20, 4}


def test_companies_from_details_is_empty_for_a_payload_with_none():
    assert companies_from_details(_details(2, [])) == set()
