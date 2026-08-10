"""The sweep's enumerate phase end to end against a mocked TMDB: what it reaches, what it
admits (nothing, until a tranche is opened), and how it behaves when TMDB misbehaves.

The admitting-nothing case is the one that ships (NEU-1077), so it is asserted as a
*catalog* fact — no film row appears — rather than by trusting a counter.
"""

from datetime import date, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from tests.fixtures.catalog import add_credit, add_film
from tests.fixtures.tmdb import make_credit_entry, make_details, make_person_movie_credits
from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.sweep import AdmissionTranches, run_sweep_enumerate

BASE_URL = "https://api.themoviedb.org/3"
EXCLUDED = frozenset({"Released", "Canceled"})
TODAY = date(2026, 8, 10)
DORMANCY_DAYS = 365
DATED = TODAY + timedelta(days=90)

DIRECTORS_ONLY = AdmissionTranches(enabled=True, directors=True)


async def _seed_director(session, person_id: int = 10, film_tmdb_id: int = 1):
    film = await add_film(session, film_tmdb_id, release_date=DATED)
    await add_credit(
        session, film, person_id, credit_type="crew", department="Directing", job="Director"
    )
    return film


def _mock_credits(person_id: int, **kwargs):
    return respx.get(f"{BASE_URL}/person/{person_id}/movie_credits").mock(
        return_value=httpx.Response(200, json=make_person_movie_credits(person_id, **kwargs))
    )


def _mock_details(tmdb_id: int, **overrides):
    return respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(
        return_value=httpx.Response(
            200, json=make_details(tmdb_id, release_date="", status="Planned", **overrides)
        )
    )


async def _run(session_factory, tmdb_client, run_id, **overrides):
    return await run_sweep_enumerate(
        session_factory=session_factory,
        client=tmdb_client,
        run_id=run_id,
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
        **{"tranches": AdmissionTranches(), **overrides},
    )


async def _run_row(session, run_id) -> IngestRun:
    return await session.get(IngestRun, run_id, populate_existing=True)


async def _film_count(session) -> int:
    return (await session.execute(select(func.count()).select_from(Film))).scalar_one()


@respx.mock
async def test_finds_candidates_and_admits_none_while_every_tranche_is_off(
    session, session_factory, tmdb_client, run_id
):
    await _seed_director(session)
    await session.commit()
    before = await _film_count(session)
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.seed_people == 1
    assert result.candidates_found == 1
    assert result.withheld == 1
    assert result.admitted == 0
    assert await _film_count(session) == before
    row = await _run_row(session, run_id)
    assert (row.items_processed, row.items_failed) == (0, 0)


@respx.mock
async def test_an_open_tranche_admits_and_counts_the_film(
    session, session_factory, tmdb_client, run_id
):
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100, title="Untitled Project")

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert (result.admitted, result.withheld) == (1, 0)
    admitted = (await session.execute(select(Film).where(Film.tmdb_id == 100))).scalar_one()
    assert admitted.title == "Untitled Project"
    assert (await _run_row(session, run_id)).items_processed == 1


@respx.mock
async def test_a_closed_tranche_withholds_a_candidate_reached_only_at_that_grade(
    session, session_factory, tmdb_client, run_id
):
    """The ramp is per seed grade: with directors open and cast still closed, a film
    reached only through a top-billed credit waits (§7.4)."""
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    _mock_credits(11, cast=[make_credit_entry(100, order=0)])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert (result.admitted, result.withheld) == (0, 1)
    assert await _film_count(session) == 1


@respx.mock
async def test_tracks_distinct_seed_people_per_candidate(
    session, session_factory, tmdb_client, run_id
):
    """`seed_attachment_count` is what the corroboration threshold will read (§4.2), so it
    counts corroborating *people* — one person credited twice is one attachment."""
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    _mock_credits(
        10,
        crew=[
            make_credit_entry(100, department="Directing", job="Director"),
            make_credit_entry(100, department="Writing", job="Writer"),
        ],
    )
    _mock_credits(11, cast=[make_credit_entry(100, order=0), make_credit_entry(101, order=1)])
    _mock_details(100)
    _mock_details(101)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.attachment_histogram == {1: 1, 2: 1}


@respx.mock
async def test_a_film_already_in_the_catalog_costs_no_details_fetch(
    session, session_factory, tmdb_client, run_id
):
    await _seed_director(session)
    await add_film(session, 500, release_date=None, status="Planned")
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(500, department="Directing", job="Director")])
    details = respx.get(f"{BASE_URL}/movie/500")

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert not details.called
    assert result.skipped_already_known == 1
    assert result.admitted == 0


@respx.mock
async def test_drops_a_candidate_the_details_reveal_to_be_dated(
    session, session_factory, tmdb_client, run_id
):
    # A credits entry can omit a release date that `/movie/{id}` carries; a dated film
    # belongs to discover, not to the sweep.
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    respx.get(f"{BASE_URL}/movie/100").mock(
        return_value=httpx.Response(
            200, json=make_details(100, status="Planned", release_date="2027-09-01")
        )
    )

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.skipped_dated_on_details == 1
    assert await _film_count(session) == 1


@pytest.mark.parametrize("status", ["Released", "Canceled"])
@respx.mock
async def test_drops_excluded_statuses_through_classify_skip(
    session, session_factory, tmdb_client, run_id, status
):
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    respx.get(f"{BASE_URL}/movie/100").mock(
        return_value=httpx.Response(200, json=make_details(100, status=status, release_date=""))
    )

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.skipped_excluded_status == 1
    assert await _film_count(session) == 1


@respx.mock
async def test_a_short_undated_film_is_not_dropped_for_its_runtime(
    session, session_factory, tmdb_client, run_id
):
    """The admission bar is status only (§4.1). Undated films rarely carry a real runtime,
    and the discover pipeline's short-film rule would reject them on a zero."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100, runtime=0)

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.admitted == 1


@respx.mock
async def test_one_unreachable_person_does_not_cost_the_rest(
    session, session_factory, tmdb_client, run_id
):
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(404))
    _mock_credits(11, cast=[make_credit_entry(100, order=0)])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.person_failures == 1
    assert result.candidates_found == 1
    assert (await _run_row(session, run_id)).items_failed == 1


@respx.mock
async def test_one_unjudgeable_candidate_does_not_roll_back_an_earlier_admission(
    session, session_factory, tmdb_client, run_id
):
    """Commits are per item, so the film admitted before the failure survives it."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(
        10,
        crew=[
            make_credit_entry(100, department="Directing", job="Director"),
            make_credit_entry(101, department="Directing", job="Director"),
        ],
    )
    _mock_details(100)
    respx.get(f"{BASE_URL}/movie/101").mock(return_value=httpx.Response(500))

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.candidate_failures == 1
    assert result.admitted == 1
    assert (await session.execute(select(Film.tmdb_id).where(Film.tmdb_id == 100))).scalar_one()
    row = await _run_row(session, run_id)
    assert (row.items_processed, row.items_failed) == (1, 1)


@respx.mock
async def test_aborts_after_consecutive_failures_and_leaves_the_run_open(
    session, session_factory, tmdb_client, run_id
):
    """A TMDB outage stops the sweep rather than burning 7,519 requests on it. The run
    stays `running`: one row covers both phases, so the terminal status is the
    entrypoint's to write (NEU-1079)."""
    film = await _seed_director(session)
    for person_id in (11, 12, 13):
        await add_credit(
            session, film, person_id, credit_type="cast", department="Acting", credit_order=0
        )
    await session.commit()
    for person_id in (10, 11, 12):
        respx.get(f"{BASE_URL}/person/{person_id}/movie_credits").mock(
            return_value=httpx.Response(500)
        )
    last = respx.get(f"{BASE_URL}/person/13/movie_credits")

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=3)

    assert result.aborted is True
    assert result.abort_error is not None
    assert not last.called, "the sweep must stop at the threshold, not merely report it"
    assert (await _run_row(session, run_id)).status == "running"


@respx.mock
async def test_a_success_resets_the_consecutive_failure_count(
    session, session_factory, tmdb_client, run_id
):
    film = await _seed_director(session)
    for person_id in (11, 12):
        await add_credit(
            session, film, person_id, credit_type="cast", department="Acting", credit_order=0
        )
    await session.commit()
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(500))
    _mock_credits(11)
    respx.get(f"{BASE_URL}/person/12/movie_credits").mock(return_value=httpx.Response(500))

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=2)

    assert result.aborted is False
    assert result.person_failures == 2


@respx.mock
async def test_survives_a_malformed_credits_payload(session, session_factory, tmdb_client, run_id):
    # A 45-minute sweep must not die on one entry TMDB sent without a title.
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(
        return_value=httpx.Response(200, json={"id": 10, "crew": [{"id": 100, "job": "Director"}]})
    )
    _mock_credits(11, cast=[make_credit_entry(101, order=0)])
    _mock_details(101)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.person_failures == 1
    assert result.candidates_found == 1
