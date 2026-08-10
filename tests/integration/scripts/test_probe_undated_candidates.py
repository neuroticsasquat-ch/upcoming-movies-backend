"""The probe end to end over a mocked TMDB. The read-only guarantee is asserted, not
assumed: the catalog is snapshotted before the run and compared after. Its two catalog
reads moved to `upmovies.ingest.sweep.seeds` (NEU-1077) and are tested there.
"""

import csv
from datetime import date, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from scripts.probe_undated_candidates import run_probe
from tests.fixtures.catalog import add_credit, add_film
from tests.fixtures.tmdb import make_credit_entry, make_details, make_person_movie_credits
from upmovies.catalog.models import Film
from upmovies.ingest.tmdb.client import TMDBClient

BASE_URL = "https://api.themoviedb.org/3"
EXCLUDED = frozenset({"Released", "Canceled"})
TODAY = date(2026, 8, 10)
DORMANCY_DAYS = 365
DATED = TODAY + timedelta(days=90)


@pytest.fixture
async def tmdb_client():
    async with TMDBClient(
        base_url=BASE_URL,
        api_key="test-key",
        rate_calls=100,
        rate_window=1,
        retry_max_attempts=2,
        retry_base_delay=0.01,
    ) as client:
        yield client


@respx.mock
async def test_run_probe_writes_the_report_and_touches_no_catalog_rows(
    session, session_factory, tmp_path, tmdb_client
):
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    before = (await session.execute(select(func.count()).select_from(Film))).scalar_one()

    # 10 directs 100 and 900; 11 is top-billed on 100 and 901. 900 falls out on status
    # once its details arrive; 901 carries a date, so it never costs a details fetch.
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(
        return_value=httpx.Response(
            200,
            json=make_person_movie_credits(
                10,
                crew=[
                    make_credit_entry(100, department="Directing", job="Director"),
                    make_credit_entry(900, department="Directing", job="Director"),
                ],
            ),
        )
    )
    respx.get(f"{BASE_URL}/person/11/movie_credits").mock(
        return_value=httpx.Response(
            200,
            json=make_person_movie_credits(
                11,
                cast=[
                    make_credit_entry(100, order=0),
                    make_credit_entry(901, order=1, release_date="2027-03-01"),
                ],
            ),
        )
    )
    respx.get(f"{BASE_URL}/movie/100").mock(
        return_value=httpx.Response(
            200,
            json=make_details(
                100,
                title="Untitled Project",
                status="Planned",
                release_date="",
                popularity=0.31,
                original_language="ko",
                runtime=0,
            ),
        )
    )
    respx.get(f"{BASE_URL}/movie/900").mock(
        return_value=httpx.Response(200, json=make_details(900, status="Released", release_date=""))
    )
    out = tmp_path / "probe.csv"

    summary = await run_probe(
        session_factory=session_factory,
        client=tmdb_client,
        out_path=out,
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
    )

    rows = list(csv.DictReader(out.open()))
    assert rows == [
        {
            "tmdb_id": "100",
            "title": "Untitled Project",
            "status": "Planned",
            "original_language": "ko",
            "popularity": "0.31",
            "seed_attachment_count": "2",
            "seed_roles_matched": "director|cast",
            "runtime": "0",
        }
    ]
    assert summary.seed_people == 2
    assert summary.candidates_reported == 1
    assert summary.skipped_excluded_status == 1
    assert (await session.execute(select(func.count()).select_from(Film))).scalar_one() == before


@respx.mock
async def test_run_probe_skips_films_already_in_the_catalog(
    session, session_factory, tmp_path, tmdb_client
):
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await add_film(session, 500, release_date=None, status="Planned")
    await session.commit()

    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(
        return_value=httpx.Response(
            200,
            json=make_person_movie_credits(
                10, crew=[make_credit_entry(500, department="Directing", job="Director")]
            ),
        )
    )
    details = respx.get(f"{BASE_URL}/movie/500")
    out = tmp_path / "probe.csv"

    summary = await run_probe(
        session_factory=session_factory,
        client=tmdb_client,
        out_path=out,
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
    )

    assert not details.called, "a film already in catalog.film must not cost a details fetch"
    assert summary.skipped_already_known == 1
    assert list(csv.DictReader(out.open())) == []


@respx.mock
async def test_run_probe_survives_a_person_whose_credits_cannot_be_fetched(
    session, session_factory, tmp_path, tmdb_client
):
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()

    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE_URL}/person/11/movie_credits").mock(
        return_value=httpx.Response(
            200, json=make_person_movie_credits(11, cast=[make_credit_entry(100, order=0)])
        )
    )
    respx.get(f"{BASE_URL}/movie/100").mock(
        return_value=httpx.Response(200, json=make_details(100, status="Planned", release_date=""))
    )
    out = tmp_path / "probe.csv"

    summary = await run_probe(
        session_factory=session_factory,
        client=tmdb_client,
        out_path=out,
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
    )

    assert summary.person_fetch_failures == 1
    assert summary.candidates_reported == 1


@respx.mock
async def test_run_probe_limit_caps_the_seed_sweep(session, session_factory, tmp_path, tmdb_client):
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()

    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(
        return_value=httpx.Response(200, json=make_person_movie_credits(10))
    )
    second = respx.get(f"{BASE_URL}/person/11/movie_credits")

    summary = await run_probe(
        session_factory=session_factory,
        client=tmdb_client,
        out_path=tmp_path / "probe.csv",
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
        limit=1,
    )

    assert not second.called
    assert summary.seed_people == 1


@respx.mock
async def test_run_probe_drops_a_candidate_the_details_reveal_to_be_dated(
    session, session_factory, tmp_path, tmdb_client
):
    # TMDB's credits summaries lag: an entry can arrive with a blank release date that
    # `/movie/{id}` fills in. Reporting it would inflate the distribution M4 tunes on.
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await session.commit()

    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(
        return_value=httpx.Response(
            200,
            json=make_person_movie_credits(
                10, crew=[make_credit_entry(100, department="Directing", job="Director")]
            ),
        )
    )
    respx.get(f"{BASE_URL}/movie/100").mock(
        return_value=httpx.Response(
            200, json=make_details(100, status="Planned", release_date="2027-09-01")
        )
    )
    out = tmp_path / "probe.csv"

    summary = await run_probe(
        session_factory=session_factory,
        client=tmdb_client,
        out_path=out,
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
    )

    assert summary.skipped_dated_on_details == 1
    assert summary.candidates_reported == 0
    assert list(csv.DictReader(out.open())) == []


@respx.mock
async def test_run_probe_survives_a_malformed_credits_payload(
    session, session_factory, tmp_path, tmdb_client
):
    # A 30-minute sweep must not die on one entry TMDB sent without a title.
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()

    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(
        return_value=httpx.Response(200, json={"id": 10, "crew": [{"id": 100, "job": "Director"}]})
    )
    respx.get(f"{BASE_URL}/person/11/movie_credits").mock(
        return_value=httpx.Response(
            200, json=make_person_movie_credits(11, cast=[make_credit_entry(101, order=0)])
        )
    )
    respx.get(f"{BASE_URL}/movie/101").mock(
        return_value=httpx.Response(200, json=make_details(101, status="Planned", release_date=""))
    )

    summary = await run_probe(
        session_factory=session_factory,
        client=tmdb_client,
        out_path=tmp_path / "probe.csv",
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
    )

    assert summary.person_fetch_failures == 1
    assert summary.candidates_reported == 1
