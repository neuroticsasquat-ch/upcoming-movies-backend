"""`/me/import/tmdb` (D-16): the approve flow's two routes, behind the entitlement gate (D-39).

The runner is covered in `tests/integration/ingest/imports/test_tmdb_runner.py` — here the task
is stubbed out, because what these tests are about is what the *routes* decide before anything
is spawned, and above all what happens to the TMDB session when they refuse."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.users import ENTITLED_UNTIL, _build_authed_client
from upmovies.app.models import ImportJob, TmdbAuthRequest
from upmovies.app.repos import tmdb_auth_repo
from upmovies.config import get_settings

BASE_URL = get_settings().tmdb_base_url.rstrip("/")


@pytest.fixture
def spawned():
    """The background task, stubbed. Nothing awaits it in production either, so letting the
    real one loose here would race the assertions and hit TMDB."""
    with patch("upmovies.routers.imports_tmdb.run_tmdb_import", new=AsyncMock()) as task:
        yield task


@pytest.fixture
def deleted():
    """`delete_session`, stubbed, so a test can assert the credential was cleaned up on a path
    that never reaches the runner."""
    with patch("upmovies.routers.imports_tmdb.delete_session", new=AsyncMock()) as fn:
        yield fn


def _mock_token(token: str = "tok-abc"):
    return respx.get(f"{BASE_URL}/authentication/token/new").mock(
        return_value=httpx.Response(200, json={"success": True, "request_token": token})
    )


def _mock_exchange(session_id: str = "sess-1", *, username: str = "cinephile") -> None:
    respx.post(f"{BASE_URL}/authentication/session/new").mock(
        return_value=httpx.Response(200, json={"success": True, "session_id": session_id})
    )
    respx.get(f"{BASE_URL}/account").mock(
        return_value=httpx.Response(200, json={"id": 42, "username": username})
    )


async def _tokens(session) -> list[TmdbAuthRequest]:
    return list((await session.execute(select(TmdbAuthRequest))).scalars().all())


async def _jobs(session) -> list[ImportJob]:
    return list((await session.execute(select(ImportJob))).scalars().all())


# --- the gate ------------------------------------------------------------------------------


async def test_start_requires_auth(client):
    assert (await client.get("/me/import/tmdb/start")).status_code == 401


async def test_callback_requires_auth(client):
    r = await client.post(
        "/me/import/tmdb/callback", json={"request_token": "tok-abc", "approved": True}
    )
    assert r.status_code == 401


@respx.mock
async def test_start_is_403_for_an_unentitled_user(authed_client, session):
    route = _mock_token()
    r = await authed_client.get("/me/import/tmdb/start", follow_redirects=False)

    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"
    # Gated in front of the request token, so an unentitled user never reaches TMDB's approve
    # screen and no token is spent (spec amendment, D-39).
    assert not route.called
    assert await _tokens(session) == []


@respx.mock
async def test_a_lapsed_entitlement_at_the_callback_deletes_the_session(
    session, make_user, spawned, deleted
):
    # The grant can end between approving on TMDB and coming back. The one-shot design means
    # nothing else would ever clean up the session that approval created, so this path has to.
    user = await make_user(email="lapsed@example.com", entitled_until=ENTITLED_UNTIL)
    await tmdb_auth_repo.create(session, request_token="tok-abc", user_id=user.id)
    await session.commit()

    user.entitled_until = datetime.now(UTC) - timedelta(days=1)
    await session.commit()

    _mock_exchange()
    async with await _build_authed_client(session, user) as c:
        r = await c.post(
            "/me/import/tmdb/callback", json={"request_token": "tok-abc", "approved": True}
        )

    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"
    deleted.assert_awaited_once()
    assert deleted.await_args.args[1] == "sess-1"
    assert await _jobs(session) == []
    spawned.assert_not_called()


# --- start ---------------------------------------------------------------------------------


@respx.mock
async def test_start_redirects_to_tmdb_with_the_configured_redirect_to(entitled_client, session):
    _mock_token()
    r = await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)

    assert r.status_code == 302
    location = httpx.URL(r.headers["location"])
    assert str(location).startswith("https://www.themoviedb.org/authenticate/tok-abc")
    assert location.params["redirect_to"] == get_settings().tmdb_redirect_url


@respx.mock
async def test_start_binds_the_token_to_the_user_who_asked_for_it(entitled_client, session):
    _mock_token()
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)

    (row,) = await _tokens(session)
    assert row.request_token == "tok-abc"
    assert row.user_id == entitled_client.user.id


@respx.mock
async def test_start_prunes_tokens_nobody_came_back_for(entitled_client, session, make_user):
    stale = await make_user(email="stale@example.com")
    await tmdb_auth_repo.create(session, request_token="tok-old", user_id=stale.id)
    await session.commit()
    row = await tmdb_auth_repo.get(session, "tok-old")
    assert row is not None
    row.created_at = datetime.now(UTC) - timedelta(hours=2)
    await session.commit()

    _mock_token()
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)

    assert [t.request_token for t in await _tokens(session)] == ["tok-abc"]


# --- the callback --------------------------------------------------------------------------


@respx.mock
async def test_an_approved_token_queues_a_job_and_spends_the_token(
    entitled_client, session, spawned
):
    _mock_token()
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)
    _mock_exchange(username="cinephile")

    r = await entitled_client.post(
        "/me/import/tmdb/callback", json={"request_token": "tok-abc", "approved": True}
    )

    assert r.status_code == 202
    (job,) = await _jobs(session)
    assert r.json() == {"job_id": str(job.id)}
    assert (job.source, job.status) == ("tmdb", "queued")
    assert job.tmdb_username == "cinephile"
    assert job.rows_total == 0  # nothing has read the library yet
    assert job.user_id == entitled_client.user.id
    # The row is deleted on use, so an approved token observed in a redirect cannot be replayed
    # even by the user it belongs to.
    assert await _tokens(session) == []
    spawned.assert_awaited_once()
    assert spawned.await_args.args[:3] == (job.id, "sess-1", 42)


@respx.mock
async def test_another_users_token_is_403_and_is_not_exchanged(
    entitled_client, session, make_user, spawned
):
    other = await make_user(email="other@example.com", entitled_until=ENTITLED_UNTIL)
    await tmdb_auth_repo.create(session, request_token="tok-theirs", user_id=other.id)
    await session.commit()
    exchange = respx.post(f"{BASE_URL}/authentication/session/new")

    r = await entitled_client.post(
        "/me/import/tmdb/callback", json={"request_token": "tok-theirs", "approved": True}
    )

    assert r.status_code == 403
    assert r.json()["detail"] == "tmdb_token_not_yours"
    assert not exchange.called
    # Refusing must not spend somebody else's pending token.
    assert [t.request_token for t in await _tokens(session)] == ["tok-theirs"]
    spawned.assert_not_called()


@respx.mock
async def test_an_unknown_token_gets_the_same_answer_as_another_users(entitled_client, spawned):
    # Distinguishing the two would tell a caller holding a token whether it is live.
    r = await entitled_client.post(
        "/me/import/tmdb/callback", json={"request_token": "tok-nope", "approved": True}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "tmdb_token_not_yours"
    spawned.assert_not_called()


@respx.mock
async def test_an_expired_token_is_400_and_is_not_exchanged(entitled_client, session, spawned):
    await tmdb_auth_repo.create(session, request_token="tok-old", user_id=entitled_client.user.id)
    await session.commit()
    row = await tmdb_auth_repo.get(session, "tok-old")
    assert row is not None
    row.created_at = datetime.now(UTC) - tmdb_auth_repo.TOKEN_TTL - timedelta(minutes=1)
    await session.commit()
    exchange = respx.post(f"{BASE_URL}/authentication/session/new")

    r = await entitled_client.post(
        "/me/import/tmdb/callback", json={"request_token": "tok-old", "approved": True}
    )

    assert r.status_code == 400
    assert r.json()["detail"] == "tmdb_token_expired"
    assert not exchange.called
    assert await _tokens(session) == []
    spawned.assert_not_called()


@respx.mock
async def test_a_refused_approval_is_400_and_never_reaches_tmdb(entitled_client, session, spawned):
    # TMDB sends the user back whether or not they approved, so a refusal arrives as a normal
    # callback carrying approved=false. There is nothing to exchange.
    _mock_token()
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)
    exchange = respx.post(f"{BASE_URL}/authentication/session/new")

    r = await entitled_client.post(
        "/me/import/tmdb/callback", json={"request_token": "tok-abc", "approved": False}
    )

    assert r.status_code == 400
    assert r.json()["detail"] == "tmdb_token_not_approved"
    assert not exchange.called
    assert await _tokens(session) == []
    assert await _jobs(session) == []
    spawned.assert_not_called()


@respx.mock
async def test_a_token_tmdb_refuses_to_exchange_is_400(entitled_client, session, spawned):
    _mock_token()
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)
    respx.post(f"{BASE_URL}/authentication/session/new").mock(
        return_value=httpx.Response(401, json={"success": False, "status_code": 3})
    )

    r = await entitled_client.post(
        "/me/import/tmdb/callback", json={"request_token": "tok-abc", "approved": True}
    )

    assert r.status_code == 400
    assert r.json()["detail"] == "tmdb_token_not_approved"
    assert await _jobs(session) == []
    spawned.assert_not_called()


@respx.mock
async def test_a_second_import_while_one_runs_is_409_and_deletes_the_session(
    entitled_client, session, spawned, deleted
):
    _mock_token()
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)
    _mock_exchange()
    assert (
        await entitled_client.post(
            "/me/import/tmdb/callback", json={"request_token": "tok-abc", "approved": True}
        )
    ).status_code == 202

    _mock_token("tok-two")
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)
    _mock_exchange("sess-2")
    r = await entitled_client.post(
        "/me/import/tmdb/callback", json={"request_token": "tok-two", "approved": True}
    )

    assert r.status_code == 409
    assert r.json()["detail"] == "import_in_progress"
    assert len(await _jobs(session)) == 1
    # The refused flow had already created a session at TMDB; nothing else would clean it up.
    deleted.assert_awaited_once()
    assert deleted.await_args.args[1] == "sess-2"


@respx.mock
async def test_the_callback_requires_the_csrf_header(entitled_client, session, spawned):
    _mock_token()
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)

    r = await entitled_client.post(
        "/me/import/tmdb/callback",
        json={"request_token": "tok-abc", "approved": True},
        headers={"X-CSRF-Token": "wrong"},
    )

    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"
    spawned.assert_not_called()


@respx.mock
async def test_the_poll_renders_the_tmdb_username(entitled_client, session, spawned):
    # The whole of what NEU-1359 gets in place of the unlink affordance the spec removed: the
    # settings screen says which account was imported, from the job row, with no credential.
    _mock_token()
    await entitled_client.get("/me/import/tmdb/start", follow_redirects=False)
    _mock_exchange(username="cinephile")
    job_id = (
        await entitled_client.post(
            "/me/import/tmdb/callback", json={"request_token": "tok-abc", "approved": True}
        )
    ).json()["job_id"]

    body = (await entitled_client.get(f"/me/import/{job_id}")).json()

    assert body["tmdb_username"] == "cinephile"
    assert body["source"] == "tmdb"
