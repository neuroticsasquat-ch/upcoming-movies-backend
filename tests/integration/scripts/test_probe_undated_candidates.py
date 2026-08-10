"""The probe's two DB reads (the seed set, the already-known films) and one end-to-end
pass over a mocked TMDB. The read-only guarantee is asserted, not assumed: the catalog is
snapshotted before the run and compared after."""

import csv
from datetime import date, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from scripts.probe_undated_candidates import (
    load_known_film_tmdb_ids,
    load_seed_person_ids,
    run_probe,
)
from tests.fixtures.tmdb import make_credit_entry, make_details, make_person_movie_credits
from upmovies.catalog.models import Film, FilmCredit, Person
from upmovies.ingest.tmdb.client import TMDBClient

BASE_URL = "https://api.themoviedb.org/3"
EXCLUDED = frozenset({"Released", "Canceled"})
TODAY = date(2026, 8, 10)


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


async def _add_film(session, tmdb_id: int, **overrides) -> Film:
    defaults = {"release_date": TODAY + timedelta(days=90), "status": "In Production"}
    film = Film(tmdb_id=tmdb_id, title=f"Film {tmdb_id}", **{**defaults, **overrides})
    session.add(film)
    await session.flush()
    return film


async def _add_credit(session, film: Film, person_id: int, **overrides) -> None:
    if await session.get(Person, person_id) is None:
        session.add(Person(id=person_id, name=f"Person {person_id}"))
        await session.flush()
    session.add(
        FilmCredit(
            credit_id=f"c-{film.tmdb_id}-{person_id}-{overrides.get('job') or 'cast'}",
            film_id=film.id,
            person_id=person_id,
            **overrides,
        )
    )
    await session.flush()


async def test_load_seed_person_ids_takes_directors_writers_and_top_five_cast(session):
    film = await _add_film(session, 1)
    await _add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await _add_credit(session, film, 11, credit_type="crew", department="Writing", job="Writer")
    await _add_credit(session, film, 12, credit_type="crew", department="Writing", job="Screenplay")
    await _add_credit(session, film, 13, credit_type="cast", department="Acting", credit_order=4)

    ids = await load_seed_person_ids(session, today=TODAY, excluded_statuses=EXCLUDED)

    assert ids == [10, 11, 12, 13]


async def test_load_seed_person_ids_excludes_weaker_credits(session):
    film = await _add_film(session, 1)
    await _add_credit(session, film, 20, credit_type="cast", department="Acting", credit_order=5)
    await _add_credit(
        session, film, 21, credit_type="crew", department="Production", job="Producer"
    )
    await _add_credit(
        session, film, 22, credit_type="crew", department="Sound", job="Original Music Composer"
    )

    assert await load_seed_person_ids(session, today=TODAY, excluded_statuses=EXCLUDED) == []


async def test_load_seed_person_ids_derives_only_from_active_films(session):
    released = await _add_film(
        session, 1, release_date=TODAY - timedelta(days=1), status="Released"
    )
    canceled = await _add_film(session, 2, status="Canceled")
    undated = await _add_film(session, 3, release_date=None, status="Planned")
    await _add_credit(session, released, 30, credit_type="crew", job="Director")
    await _add_credit(session, canceled, 31, credit_type="crew", job="Director")
    await _add_credit(session, undated, 32, credit_type="crew", job="Director")

    assert await load_seed_person_ids(session, today=TODAY, excluded_statuses=EXCLUDED) == [32]


async def test_load_seed_person_ids_is_distinct_across_films(session):
    for tmdb_id in (1, 2):
        film = await _add_film(session, tmdb_id)
        await _add_credit(session, film, 40, credit_type="crew", job="Director")

    assert await load_seed_person_ids(session, today=TODAY, excluded_statuses=EXCLUDED) == [40]


async def test_load_known_film_tmdb_ids_includes_inactive_films(session):
    await _add_film(session, 1)
    await _add_film(session, 2, release_date=TODAY - timedelta(days=400), status="Released")

    assert await load_known_film_tmdb_ids(session) == {1, 2}


@respx.mock
async def test_run_probe_writes_the_report_and_touches_no_catalog_rows(
    session, session_factory, tmp_path, tmdb_client
):
    film = await _add_film(session, 1)
    await _add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await _add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
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
    film = await _add_film(session, 1)
    await _add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await _add_film(session, 500, release_date=None, status="Planned")
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
    )

    assert not details.called, "a film already in catalog.film must not cost a details fetch"
    assert summary.skipped_already_known == 1
    assert list(csv.DictReader(out.open())) == []


@respx.mock
async def test_run_probe_survives_a_person_whose_credits_cannot_be_fetched(
    session, session_factory, tmp_path, tmdb_client
):
    film = await _add_film(session, 1)
    await _add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await _add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
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
    )

    assert summary.person_fetch_failures == 1
    assert summary.candidates_reported == 1


@respx.mock
async def test_run_probe_limit_caps_the_seed_sweep(session, session_factory, tmp_path, tmdb_client):
    film = await _add_film(session, 1)
    await _add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await _add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
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
    film = await _add_film(session, 1)
    await _add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
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
    )

    assert summary.skipped_dated_on_details == 1
    assert summary.candidates_reported == 0
    assert list(csv.DictReader(out.open())) == []


@respx.mock
async def test_run_probe_survives_a_malformed_credits_payload(
    session, session_factory, tmp_path, tmdb_client
):
    # A 30-minute sweep must not die on one entry TMDB sent without a title.
    film = await _add_film(session, 1)
    await _add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await _add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
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
    )

    assert summary.person_fetch_failures == 1
    assert summary.candidates_reported == 1
