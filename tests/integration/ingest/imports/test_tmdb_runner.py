"""The TMDB account import runner (D-16): what a watchlist and a favorites list turn into, what
a second run does not, and that the session is always deleted.

The acceptance criteria of `NEU-1357-tmdb-account-import.md` in order, driven through
`import_tmdb_account` against a respx-mocked TMDB. The ids are grouped so a film's role is
readable from its number: 10xx is watchlisted, 20xx is favorited, 1xx directs, 2xx acts."""

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow, ImportJob, WatchlistDismissal, WatchlistItem
from upmovies.app.repos import import_job_repo
from upmovies.catalog.models import Film, Person
from upmovies.config import get_settings
from upmovies.ingest.imports.tmdb_account import (
    SOURCE,
    import_tmdb_account,
    run_tmdb_import,
)
from upmovies.ingest.tmdb.client import TMDBClient

BASE_URL = get_settings().tmdb_base_url.rstrip("/")
ACCOUNT_ID = 42
SESSION_ID = "sess-1"

# Two watchlist movies and two favorites, one of which (1002) is on both lists — the overlap
# the spec calls out, which must get both treatments.
WATCHLIST_IDS = (1001, 1002)
FAVORITE_IDS = (1002, 2001)

# Director, then billing slots 0, 1 and 2. Slot 2 is present in every payload and must never
# become a follow.
CREDITS = {
    1001: {"director": 100, "cast": [200, 201, 202]},
    1002: {"director": 101, "cast": [210, 211, 212]},
    2001: {"director": 102, "cast": [220, 221, 222]},
}
# The people of the two favorites — 1002 and 2001 — and nobody else's.
PROMOTED_PEOPLE = {101, 210, 211, 102, 220, 221}
EXCLUDED_PEOPLE = {202, 212, 222}


def _credits(director_id: int, cast_ids: list[int]) -> dict:
    return {
        "crew": [
            {
                "id": director_id,
                "name": f"Director {director_id}",
                "credit_id": f"crew-{director_id}",
                "department": "Directing",
                "job": "Director",
            }
        ],
        "cast": [
            {
                "id": person_id,
                "name": f"Actor {person_id}",
                "credit_id": f"cast-{person_id}",
                "order": order,
            }
            for order, person_id in enumerate(cast_ids)
        ],
    }


def _list_page(tmdb_ids) -> dict:
    return {
        "page": 1,
        "total_pages": 1,
        "total_results": len(tmdb_ids),
        "results": [
            {"id": i, "title": f"Film {i}", "release_date": "2021-06-01", "popularity": 10.0}
            for i in tmdb_ids
        ],
    }


def _mock_lists(watchlist=WATCHLIST_IDS, favorites=FAVORITE_IDS) -> None:
    respx.get(f"{BASE_URL}/account/{ACCOUNT_ID}/watchlist/movies").mock(
        return_value=httpx.Response(200, json=_list_page(watchlist))
    )
    respx.get(f"{BASE_URL}/account/{ACCOUNT_ID}/favorite/movies").mock(
        return_value=httpx.Response(200, json=_list_page(favorites))
    )


def _mock_details() -> None:
    for tmdb_id, roles in CREDITS.items():
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(
            return_value=httpx.Response(
                200,
                json=make_details(
                    tmdb_id, credits=_credits(roles["director"], list(roles["cast"]))
                ),
            )
        )


def _mock_delete():
    return respx.delete(f"{BASE_URL}/authentication/session").mock(
        return_value=httpx.Response(200, json={"success": True})
    )


def _mock_tmdb() -> None:
    _mock_lists()
    _mock_details()
    _mock_delete()


def _client() -> TMDBClient:
    settings = get_settings()
    return TMDBClient(
        base_url=settings.tmdb_base_url,
        api_key="test-key",
        rate_calls=1000,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.01,
    )


@pytest.fixture
async def user(make_user):
    return await make_user(email="tmdb-importer@example.com")


async def _queue(session, user) -> ImportJob:
    job = await import_job_repo.create(
        session, user_id=user.id, source=SOURCE, rows_total=0, tmdb_username="cinephile"
    )
    await session.commit()
    return job


async def _run(session, session_factory, user) -> ImportJob:
    job = await _queue(session, user)
    async with _client() as client:
        await import_tmdb_account(
            session_factory=session_factory,
            client=client,
            job_id=job.id,
            session_id=SESSION_ID,
            account_id=ACCOUNT_ID,
        )
    return await session.get(ImportJob, job.id, populate_existing=True)


async def _rows(session, model) -> list:
    return list((await session.execute(select(model))).scalars().all())


# --- the whole account ----------------------------------------------------------------------


@respx.mock
async def test_the_two_lists_produce_the_expected_films_follows_and_watchlist(
    session, session_factory, user
):
    _mock_tmdb()
    job = await _run(session, session_factory, user)

    assert job.status == "succeeded"
    assert job.error is None
    # Both lists, counted once the runner had read them — nothing knew the total before.
    assert job.rows_done == job.rows_total == len(WATCHLIST_IDS) + len(FAVORITE_IDS)

    # No resolution step, so nothing can go unplaced: the ids are authoritative.
    assert job.unmatched == []

    assert job.watchlist_created == 2
    items = await _rows(session, WatchlistItem)
    assert {item.source for item in items} == {"tmdb_import"}
    assert {tuple(item.alert_prefs) for item in items} == {("stream",)}

    follows = await _rows(session, Follow)
    assert {f.source for f in follows} == {"tmdb_import"}
    # Two title follows, plus the distinct people of the two favorites.
    assert job.follows_created == len(follows) == 2 + len(PROMOTED_PEOPLE)
    assert {f.entity_id for f in follows if f.entity_type == "person"} == {
        str(p) for p in PROMOTED_PEOPLE
    }


@respx.mock
async def test_a_favorite_only_film_contributes_people_but_never_a_catalog_row(
    session, session_factory, user
):
    # The catalog is the upcoming-film spine: a film someone has favorited has nothing left to
    # announce, so it is fetched for its credits and discarded (spec §3).
    _mock_tmdb()
    await _run(session, session_factory, user)

    films = {f.tmdb_id for f in await _rows(session, Film)}
    assert films == set(WATCHLIST_IDS)
    assert 2001 not in films

    people = {p.id for p in await _rows(session, Person)}
    assert PROMOTED_PEOPLE <= people


@respx.mock
async def test_a_film_on_both_lists_gets_both_treatments(session, session_factory, user):
    # 1002 is watchlisted *and* favorited: the film is what they want telling about, and its
    # people are what the favorite says about their taste (spec §3).
    _mock_tmdb()
    await _run(session, session_factory, user)

    film = (await session.execute(select(Film).where(Film.tmdb_id == 1002))).scalar_one()
    follows = await _rows(session, Follow)
    assert str(film.id) in {f.entity_id for f in follows if f.entity_type == "title"}
    assert film.id in {item.film_id for item in await _rows(session, WatchlistItem)}
    assert {"101", "210", "211"} <= {f.entity_id for f in follows if f.entity_type == "person"}


@respx.mock
async def test_billing_below_the_top_two_is_not_followed(session, session_factory, user):
    _mock_tmdb()
    await _run(session, session_factory, user)

    followed = {f.entity_id for f in await _rows(session, Follow) if f.entity_type == "person"}
    assert followed.isdisjoint({str(p) for p in EXCLUDED_PEOPLE})


@respx.mock
async def test_a_film_tmdb_has_deleted_is_reported_rather_than_failing_the_job(
    session, session_factory, user
):
    # The one thing that can still go wrong when the ids are authoritative: the user's own list
    # names an entry TMDB has since removed.
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1001").mock(return_value=httpx.Response(404))

    job = await _run(session, session_factory, user)

    assert job.status == "succeeded"
    assert job.unmatched == [{"name": "Film 1001", "year": 2021, "kind": "tmdb_missing"}]
    # The other three rows went through.
    assert job.rows_done == 4
    assert {f.tmdb_id for f in await _rows(session, Film)} == {1002}


# --- running it twice ------------------------------------------------------------------------


@respx.mock
async def test_re_running_the_same_account_creates_nothing_new(session, session_factory, user):
    _mock_tmdb()
    await _run(session, session_factory, user)
    before = (len(await _rows(session, Follow)), len(await _rows(session, WatchlistItem)))

    second = await _run(session, session_factory, user)

    assert second.status == "succeeded"
    assert (second.follows_created, second.watchlist_created) == (0, 0)
    assert (len(await _rows(session, Follow)), len(await _rows(session, WatchlistItem))) == before


@respx.mock
async def test_a_dismissed_film_is_not_put_back_on_the_watchlist(session, session_factory, user):
    # D-13, exactly as for the Letterboxd import: an import is not a reason to overrule a
    # removal the user already made.
    _mock_tmdb()
    await _run(session, session_factory, user)
    film = (await session.execute(select(Film).where(Film.tmdb_id == 1001))).scalar_one()
    for item in await _rows(session, WatchlistItem):
        if item.film_id == film.id:
            await session.delete(item)
    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()

    await _run(session, session_factory, user)

    assert film.id not in {item.film_id for item in await _rows(session, WatchlistItem)}
    # The follow is still there — they listed the film, and a follow is a timeline row.
    assert str(film.id) in {f.entity_id for f in await _rows(session, Follow)}


# --- the session ------------------------------------------------------------------------------


@respx.mock
async def test_the_session_is_deleted_exactly_once_when_the_import_succeeds(
    session, session_factory, user
):
    _mock_tmdb()
    deleted = _mock_delete()
    job = await _queue(session, user)

    await run_tmdb_import(job.id, SESSION_ID, ACCOUNT_ID, get_settings())

    finished = await session.get(ImportJob, job.id, populate_existing=True)
    assert finished.status == "succeeded"
    assert len(deleted.calls) == 1
    assert json.loads(deleted.calls.last.request.read()) == {"session_id": SESSION_ID}


@respx.mock
async def test_the_session_is_deleted_even_when_a_page_fetch_raises(session, session_factory, user):
    # The acceptance criterion the one-shot design turns on: nothing else in the system will
    # ever clean up this credential, so a crash must not be what skips the delete.
    _mock_details()
    deleted = _mock_delete()
    respx.get(f"{BASE_URL}/account/{ACCOUNT_ID}/watchlist/movies").mock(
        return_value=httpx.Response(500)
    )
    job = await _queue(session, user)

    await run_tmdb_import(
        job.id,
        SESSION_ID,
        ACCOUNT_ID,
        get_settings().model_copy(update={"tmdb_retry_max_attempts": 1}),
    )

    finished = await session.get(ImportJob, job.id, populate_existing=True)
    assert finished.status == "failed"
    assert finished.error
    assert len(deleted.calls) == 1


@respx.mock
async def test_a_delete_that_fails_is_logged_rather_than_failing_the_import(
    session, session_factory, user
):
    # By this point the import has done everything it was asked to do, and TMDB expires an
    # unused session on its own — so "could not log out" must not become the job's outcome.
    _mock_lists()
    _mock_details()
    respx.delete(f"{BASE_URL}/authentication/session").mock(return_value=httpx.Response(500))
    job = await _queue(session, user)

    await run_tmdb_import(
        job.id,
        SESSION_ID,
        ACCOUNT_ID,
        get_settings().model_copy(update={"tmdb_retry_max_attempts": 1}),
    )

    finished = await session.get(ImportJob, job.id, populate_existing=True)
    assert finished.status == "succeeded"
    assert finished.error is None


@respx.mock
async def test_a_crash_mid_run_fails_the_job_and_keeps_the_rows_already_done(
    session, session_factory, user
):
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1002").mock(return_value=httpx.Response(500))
    job = await _queue(session, user)

    await run_tmdb_import(
        job.id,
        SESSION_ID,
        ACCOUNT_ID,
        get_settings().model_copy(update={"tmdb_retry_max_attempts": 1}),
    )

    finished = await session.get(ImportJob, job.id, populate_existing=True)
    assert finished.status == "failed"
    assert finished.error
    # Commit-per-row is what makes a partial import worth keeping.
    assert {f.tmdb_id for f in await _rows(session, Film)} == {1001}
    assert len(await _rows(session, WatchlistItem)) == 1


@respx.mock
async def test_a_teardown_failure_after_a_good_import_does_not_fail_the_job(
    session, session_factory, user
):
    # `import_job_repo.finalize` is unconditional, so anything that reaches the wrapper's
    # `except` after the import already finalized would rewrite a finished job as `failed`.
    # Closing a connection pool has nothing left to say about whether the import worked.
    _mock_tmdb()
    job = await _queue(session, user)

    with patch.object(TMDBClient, "__aexit__", side_effect=RuntimeError("pool teardown blew up")):
        await run_tmdb_import(job.id, SESSION_ID, ACCOUNT_ID, get_settings())

    finished = await session.get(ImportJob, job.id, populate_existing=True)
    assert finished.status == "succeeded"
    assert finished.error is None
