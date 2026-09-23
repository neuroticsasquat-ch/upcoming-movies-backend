"""The TMDB account import runner (D-16): what an account watchlist turns into, what a second
run does not, and that the session is always deleted.

The acceptance criteria of `NEU-1357-tmdb-account-import.md` as EF-20 and EF-21 leave them,
driven through `import_tmdb_account` against a respx-mocked TMDB. The favorites half is gone —
the list is not read at all — and a watchlisted film outside the alert window is reported
rather than followed."""

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow, ImportJob
from upmovies.app.repos import import_job_repo
from upmovies.catalog.models import Film
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

# Three watchlist movies, one of them released in 2019 and so outside the alert window
# (EF-21), and two favorites that nothing must ever read (EF-20).
WATCHLIST_IDS = (1001, 1002, 1003)
FAVORITE_IDS = (2001, 2002)
OUTSIDE_WINDOW_TMDB_ID = 1003

DETAILS: dict[int, tuple[str, str]] = {
    1001: ("2099-06-01", "Post Production"),
    1002: ("2099-09-01", "Planned"),
    1003: ("2019-06-01", "Released"),
    2001: ("1995-12-15", "Released"),
    2002: ("1999-11-05", "Released"),
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


def _mock_watchlist(tmdb_ids=WATCHLIST_IDS):
    return respx.get(f"{BASE_URL}/account/{ACCOUNT_ID}/watchlist/movies").mock(
        return_value=httpx.Response(200, json=_list_page(tmdb_ids))
    )


def _mock_favorites():
    """Routed so that reading it would be a *visible* failure. If the import ever asks for the
    favorites again (EF-20), `favorites.calls` says so — an unrouted respx request raises, but
    a raise inside the runner only turns into a failed job, which is a much vaguer signal."""
    return respx.get(f"{BASE_URL}/account/{ACCOUNT_ID}/favorite/movies").mock(
        return_value=httpx.Response(200, json=_list_page(FAVORITE_IDS))
    )


def _credits(tmdb_id: int) -> dict:
    """A director and three billed cast for one film, ids derived from its own.

    Present in every payload on purpose. The deleted people path followed a film's director
    and top-2 billing, so a payload with no credits in it would make
    `test_an_import_never_writes_a_person_follow` pass for the wrong reason — there would be
    nobody to follow. It also gives the film a `credits_observed_at`, which is what the
    freshness short-circuit in `film_id_for` reads on a second run."""
    director = tmdb_id * 10
    return {
        "crew": [
            {
                "id": director,
                "name": f"Director {director}",
                "credit_id": f"crew-{director}",
                "department": "Directing",
                "job": "Director",
            }
        ],
        "cast": [
            {
                "id": director + 1 + order,
                "name": f"Actor {director + 1 + order}",
                "credit_id": f"cast-{director + 1 + order}",
                "order": order,
            }
            for order in range(3)
        ],
    }


def _mock_details() -> None:
    for tmdb_id, (release_date, status) in DETAILS.items():
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(
            return_value=httpx.Response(
                200,
                json=make_details(
                    tmdb_id,
                    release_date=release_date,
                    status=status,
                    credits=_credits(tmdb_id),
                ),
            )
        )


def _mock_delete():
    return respx.delete(f"{BASE_URL}/authentication/session").mock(
        return_value=httpx.Response(200, json={"success": True})
    )


def _mock_tmdb() -> None:
    _mock_watchlist()
    _mock_favorites()
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


async def _follows(session) -> list[Follow]:
    return await _rows(session, Follow)


# --- the watchlist ----------------------------------------------------------------------------


@respx.mock
async def test_the_watchlist_produces_title_follows_and_a_report(session, session_factory, user):
    _mock_tmdb()
    job = await _run(session, session_factory, user)

    assert job.status == "succeeded"
    assert job.error is None
    # The watchlist alone, counted once the runner had read it — nothing knew the total before.
    assert job.rows_done == job.rows_total == len(WATCHLIST_IDS)

    assert (job.watchlist_created, job.follows_created) == (2, 2)
    follows = await _follows(session)
    assert len(follows) == 2
    assert {f.entity_type for f in follows} == {"title"}
    assert {f.source for f in follows} == {"tmdb_import"}

    # The ids are authoritative, so the only row in the report is the one the window closed on.
    assert job.unmatched == [{"name": "Film 1003", "year": 2021, "kind": "outside_window"}]


@respx.mock
async def test_the_favorites_list_is_never_read(session, session_factory, user):
    """EF-20. It used to buy person follows for each favorite's director and top-2 billing;
    a follow is binary now (EF-1), so a film favorited years ago would push every credit
    change of its cast at the user."""
    _mock_tmdb()
    favorites = _mock_favorites()

    await _run(session, session_factory, user)

    assert len(favorites.calls) == 0
    assert {f.tmdb_id for f in await _rows(session, Film)}.isdisjoint(FAVORITE_IDS)


@respx.mock
async def test_an_import_never_writes_a_person_follow(session, session_factory, user):
    _mock_tmdb()
    await _run(session, session_factory, user)

    assert [f for f in await _follows(session) if f.entity_type != "title"] == []


# --- the alert window --------------------------------------------------------------------------


@respx.mock
async def test_a_film_released_in_2019_is_skipped_as_outside_window(session, session_factory, user):
    """EF-21, and the reason the report row names a cause rather than a list: the film *is*
    there, and the user can go and look at it — the import simply has nothing to deliver on
    it."""
    _mock_tmdb()
    job = await _run(session, session_factory, user)

    film = (
        await session.execute(select(Film).where(Film.tmdb_id == OUTSIDE_WINDOW_TMDB_ID))
    ).scalar_one()
    assert str(film.id) not in {f.entity_id for f in await _follows(session)}
    assert job.unmatched == [{"name": "Film 1003", "year": 2021, "kind": "outside_window"}]
    # The name and year are the *list payload's*, not the catalog's: they are what the user
    # sees on the TMDB page they are looking at.
    assert film.release_date.year == 2019


@respx.mock
async def test_a_film_outside_the_window_is_still_upserted(session, session_factory, user):
    _mock_tmdb()
    await _run(session, session_factory, user)

    assert OUTSIDE_WINDOW_TMDB_ID in {f.tmdb_id for f in await _rows(session, Film)}


@respx.mock
async def test_a_canceled_film_is_skipped_however_recent_its_date(session, session_factory, user):
    # `ALERT_WINDOW_DEAD_STATUSES` is `Canceled` alone (NEU-1417): a film called off next year
    # has a date well inside the ceiling and nothing left to say.
    _mock_tmdb()
    _mock_watchlist((1001,))
    respx.get(f"{BASE_URL}/movie/1001").mock(
        return_value=httpx.Response(
            200,
            json=make_details(
                1001, release_date="2099-06-01", status="Canceled", credits=_credits(1001)
            ),
        )
    )

    job = await _run(session, session_factory, user)

    assert await _follows(session) == []
    assert job.unmatched == [{"name": "Film 1001", "year": 2021, "kind": "outside_window"}]


@respx.mock
async def test_a_film_tmdb_has_deleted_is_reported_rather_than_failing_the_job(
    session, session_factory, user
):
    # The one resolution failure left when the ids are authoritative: the user's own list names
    # an entry TMDB has since removed. It keeps its own kind, because "we cannot find it" and
    # "there is nothing left on it" are different things to go and do something about.
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1001").mock(return_value=httpx.Response(404))

    job = await _run(session, session_factory, user)

    assert job.status == "succeeded"
    assert {"name": "Film 1001", "year": 2021, "kind": "tmdb_missing"} in job.unmatched
    # The other two rows went through.
    assert job.rows_done == 3
    assert {f.tmdb_id for f in await _rows(session, Film)} == {1002, 1003}


# --- running it twice ------------------------------------------------------------------------


@respx.mock
async def test_re_running_the_same_account_creates_nothing_new(session, session_factory, user):
    _mock_tmdb()
    first = await _run(session, session_factory, user)
    before = len(await _follows(session))

    second = await _run(session, session_factory, user)

    assert second.status == "succeeded"
    assert (second.follows_created, second.watchlist_created) == (0, 0)
    assert len(await _follows(session)) == before
    assert second.unmatched == first.unmatched


@respx.mock
async def test_a_second_import_leaves_the_follow_it_already_wrote(session, session_factory, user):
    # Exactly as for the Letterboxd import: the follow is idempotent, and it is now the whole
    # of what an import writes for a listed film (EF-14).
    _mock_tmdb()
    await _run(session, session_factory, user)
    film = (await session.execute(select(Film).where(Film.tmdb_id == 1001))).scalar_one()

    await _run(session, session_factory, user)

    assert str(film.id) in {f.entity_id for f in await _follows(session)}


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
    _mock_watchlist()
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
    assert len([f for f in await _follows(session) if f.entity_type == "title"]) == 1


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
