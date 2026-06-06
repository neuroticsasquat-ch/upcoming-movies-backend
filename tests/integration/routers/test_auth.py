"""Integration tests for auth routes.

Merges tests from tests/test_auth_routes.py and the auth handler tests from
tests/test_route_handlers.py.
"""

import pytest
from fastapi import HTTPException, Request, Response
from httpx import ASGITransport, AsyncClient

from upmovies.app.dto import (
    LoginRequest,
    PasswordChangeRequest,
    SignupRequest,
)
from upmovies.app.services import account_service
from upmovies.config import get_settings
from upmovies.main import app
from upmovies.routers import auth as auth_router


@pytest.fixture
async def client(session):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Route-level tests (via ASGITransport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_creates_user_and_sets_cookies(client, make_invite):
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json={
            "email": "Alice@example.com",
            "password": "hunter2hunter2",
            "display_name": "Alice",
            "invite_code": invite,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "Alice@example.com"
    assert body["display_name"] == "Alice"
    assert "id" in body
    cookies = {c.name: c.value for c in r.cookies.jar}
    assert "upmovies_session" in cookies
    assert "csrf_token" in cookies


@pytest.mark.asyncio
async def test_signup_rejects_duplicate_email_case_insensitive(client, make_invite):
    invite1 = await make_invite()
    invite2 = await make_invite()
    r1 = await client.post(
        "/auth/signup",
        json={
            "email": "bob@example.com",
            "password": "hunter2hunter2",
            "display_name": "Bob",
            "invite_code": invite1,
        },
    )
    assert r1.status_code == 201
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c2:
        r2 = await c2.post(
            "/auth/signup",
            json={
                "email": "BOB@example.com",
                "password": "hunter2hunter2",
                "display_name": "Bob2",
                "invite_code": invite2,
            },
        )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "email_in_use"


@pytest.mark.asyncio
async def test_signup_rejects_short_password(client):
    r = await client.post(
        "/auth/signup",
        json={
            "email": "c@example.com",
            "password": "short",
            "display_name": "C",
            "invite_code": "anything",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_signup_rejects_invalid_email(client):
    r = await client.post(
        "/auth/signup",
        json={
            "email": "not-an-email",
            "password": "hunter2hunter2",
            "display_name": "X",
            "invite_code": "anything",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_signup_rejects_invalid_invite(client):
    r = await client.post(
        "/auth/signup",
        json={
            "email": "noinvite@example.com",
            "password": "hunter2hunter2",
            "display_name": "NoInvite",
            "invite_code": "this-code-does-not-exist",
        },
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "invalid_invite"


@pytest.mark.asyncio
async def test_signup_rejects_consumed_invite(client, make_invite):
    invite = await make_invite()
    r1 = await client.post(
        "/auth/signup",
        json={
            "email": "first@example.com",
            "password": "hunter2hunter2",
            "display_name": "First",
            "invite_code": invite,
        },
    )
    assert r1.status_code == 201
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c2:
        r2 = await c2.post(
            "/auth/signup",
            json={
                "email": "second@example.com",
                "password": "hunter2hunter2",
                "display_name": "Second",
                "invite_code": invite,
            },
        )
    assert r2.status_code == 403
    assert r2.json()["detail"] == "invalid_invite"


@pytest.mark.asyncio
async def test_signup_rejects_email_hint_mismatch(client, make_invite):
    invite = await make_invite(email_hint="alice@example.com")
    r = await client.post(
        "/auth/signup",
        json={
            "email": "bob@example.com",
            "password": "hunter2hunter2",
            "display_name": "Bob",
            "invite_code": invite,
        },
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "invalid_invite"


@pytest.mark.asyncio
async def test_login_succeeds_with_correct_credentials(client, make_invite):
    invite = await make_invite()
    await client.post(
        "/auth/signup",
        json={
            "email": "lo@example.com",
            "password": "hunter2hunter2",
            "display_name": "Lo",
            "invite_code": invite,
        },
    )
    csrf = client.cookies["csrf_token"]
    await client.post("/auth/logout", headers={"X-CSRF-Token": csrf})

    r = await client.post(
        "/auth/login",
        json={"email": "lo@example.com", "password": "hunter2hunter2"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "lo@example.com"
    assert "upmovies_session" in {c.name for c in r.cookies.jar}


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client, make_invite):
    invite = await make_invite()
    await client.post(
        "/auth/signup",
        json={
            "email": "wp@example.com",
            "password": "hunter2hunter2",
            "display_name": "WP",
            "invite_code": invite,
        },
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c2:
        r = await c2.post(
            "/auth/login",
            json={"email": "wp@example.com", "password": "wrong"},
        )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_login_rejects_unknown_email(client):
    r = await client.post(
        "/auth/login",
        json={"email": "ghost@example.com", "password": "hunter2hunter2"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_logout_clears_cookies_and_invalidates_session(client, make_invite):
    invite = await make_invite()
    await client.post(
        "/auth/signup",
        json={
            "email": "out@example.com",
            "password": "hunter2hunter2",
            "display_name": "Out",
            "invite_code": invite,
        },
    )
    csrf = client.cookies["csrf_token"]
    r = await client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    set_cookie_headers = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    assert any("upmovies_session=" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_logout_requires_csrf(client, make_invite):
    invite = await make_invite()
    await client.post(
        "/auth/signup",
        json={
            "email": "cs@example.com",
            "password": "hunter2hunter2",
            "display_name": "CS",
            "invite_code": invite,
        },
    )
    r = await client.post("/auth/logout")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_change_password_requires_correct_current_password(client, make_invite):
    invite = await make_invite()
    await client.post(
        "/auth/signup",
        json={
            "email": "pc@example.com",
            "password": "hunter2hunter2",
            "display_name": "PC",
            "invite_code": invite,
        },
    )
    csrf = client.cookies["csrf_token"]
    r = await client.post(
        "/auth/password",
        headers={"X-CSRF-Token": csrf},
        json={"current_password": "wrong", "new_password": "newpassword99"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_change_password_rotates_session(client, make_invite):
    invite = await make_invite()
    await client.post(
        "/auth/signup",
        json={
            "email": "rot@example.com",
            "password": "hunter2hunter2",
            "display_name": "Rot",
            "invite_code": invite,
        },
    )
    old_session = client.cookies["upmovies_session"]
    csrf = client.cookies["csrf_token"]
    r = await client.post(
        "/auth/password",
        headers={"X-CSRF-Token": csrf},
        json={"current_password": "hunter2hunter2", "new_password": "newpassword99"},
    )
    assert r.status_code == 200
    assert "csrf_token" in r.json()
    new_session = client.cookies["upmovies_session"]
    assert new_session != old_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c2:
        bad = await c2.post(
            "/auth/login",
            json={"email": "rot@example.com", "password": "hunter2hunter2"},
        )
        assert bad.status_code == 401
        good = await c2.post(
            "/auth/login",
            json={"email": "rot@example.com", "password": "newpassword99"},
        )
        assert good.status_code == 200


# ---------------------------------------------------------------------------
# Direct route handler tests (from test_route_handlers.py)
# ---------------------------------------------------------------------------


def _request(*, cookies: dict[str, str] | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_signup_route_returns_authed_user_and_sets_cookies(session, make_invite):
    invite = await make_invite()
    request = _request()
    response = Response()
    settings = get_settings()
    payload = SignupRequest(
        email="signup@example.com",
        password="hunter2hunter2",
        display_name="Sign",
        invite_code=invite,
    )
    result = await auth_router.signup(payload, request, response, db=session, settings=settings)
    assert result.email == "signup@example.com"
    assert result.csrf_token
    set_cookie_headers = response.headers.getlist("set-cookie")
    assert any("upmovies_session=" in h for h in set_cookie_headers)
    assert any("csrf_token=" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_signup_route_raises_409_on_duplicate_email(session, make_user, make_invite):
    await make_user(email="dup@example.com")
    invite = await make_invite()
    request = _request()
    response = Response()
    settings = get_settings()
    payload = SignupRequest(
        email="dup@example.com",
        password="hunter2hunter2",
        display_name="Dup",
        invite_code=invite,
    )
    with pytest.raises(HTTPException) as ei:
        await auth_router.signup(payload, request, response, db=session, settings=settings)
    assert ei.value.status_code == 409
    assert ei.value.detail == "email_in_use"


@pytest.mark.asyncio
async def test_login_route_returns_authed_user(session, make_user):
    await make_user(email="lo-rt@example.com", password="hunter2hunter2")
    request = _request()
    response = Response()
    settings = get_settings()
    payload = LoginRequest(email="lo-rt@example.com", password="hunter2hunter2")
    result = await auth_router.login(payload, request, response, db=session, settings=settings)
    assert result.email == "lo-rt@example.com"
    assert result.csrf_token


@pytest.mark.asyncio
async def test_login_route_raises_401_on_bad_password(session, make_user):
    await make_user(email="bad-rt@example.com", password="hunter2hunter2")
    request = _request()
    response = Response()
    settings = get_settings()
    payload = LoginRequest(email="bad-rt@example.com", password="wrong")
    with pytest.raises(HTTPException) as ei:
        await auth_router.login(payload, request, response, db=session, settings=settings)
    assert ei.value.status_code == 401
    assert ei.value.detail == "invalid_credentials"


@pytest.mark.asyncio
async def test_logout_route_clears_cookies(session, make_user):
    await make_user(email="lo-out@example.com")
    _, sess_id, _ = await account_service.authenticate(
        session,
        email="lo-out@example.com",
        password="hunter2hunter2",
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    request = _request(cookies={"upmovies_session": sess_id})
    response = Response()
    settings = get_settings()
    out = await auth_router.logout(request, response, db=session, settings=settings)
    assert out.status_code == 204
    set_cookie_headers = response.headers.getlist("set-cookie")
    # Both cookies are cleared (set with Max-Age=0 / past expiry).
    assert any("upmovies_session=" in h for h in set_cookie_headers)
    assert any("csrf_token=" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_logout_route_no_cookie_still_clears(session):
    """Logging out without an active session is still a successful no-op."""
    request = _request()
    response = Response()
    settings = get_settings()
    out = await auth_router.logout(request, response, db=session, settings=settings)
    assert out.status_code == 204


@pytest.mark.asyncio
async def test_change_password_route_returns_new_authed_user(session, make_user):
    user = await make_user(email="cp-rt@example.com", password="hunter2hunter2")
    request = _request()
    response = Response()
    settings = get_settings()
    payload = PasswordChangeRequest(
        current_password="hunter2hunter2",
        new_password="newpassword99",
    )
    result = await auth_router.change_password(
        payload, request, response, user=user, db=session, settings=settings
    )
    assert result.csrf_token
    set_cookie_headers = response.headers.getlist("set-cookie")
    assert any("upmovies_session=" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_change_password_route_raises_401_on_wrong_current(session, make_user):
    user = await make_user(email="cp-wrong-rt@example.com", password="hunter2hunter2")
    request = _request()
    response = Response()
    settings = get_settings()
    payload = PasswordChangeRequest(current_password="wrong", new_password="newpassword99")
    with pytest.raises(HTTPException) as ei:
        await auth_router.change_password(
            payload, request, response, user=user, db=session, settings=settings
        )
    assert ei.value.status_code == 401
