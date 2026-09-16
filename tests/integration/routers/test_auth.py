"""Integration tests for auth routes.

Merges tests from tests/test_auth_routes.py and the auth handler tests from
tests/test_route_handlers.py.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import HTTPException, Request, Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from upmovies.app.dto import (
    LoginRequest,
    PasswordChangeRequest,
    SignupRequest,
)
from upmovies.app.models import Invite, User
from upmovies.app.services import account_service
from upmovies.app.turnstile import TurnstileUnavailable
from upmovies.config import get_settings
from upmovies.deps import get_turnstile
from upmovies.mail import MailGateway, NoopTransport
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
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
                "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
            "invite_code": "anything",
        },
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_signup_rejects_a_display_name_carrying_a_newline(client):
    """A display name is one line of text, and every transactional mail interpolates it into a
    plain-text body where autoescape is off by design. Since NEU-1341 one of those mails goes
    to an address the caller merely names, so a newline here is a way to put arbitrary prose in
    front of a stranger over the product's own sending domain."""
    r = await client.post(
        "/auth/signup",
        json={
            "email": "injected@example.com",
            "password": "hunter2hunter2",
            "display_name": "Ada\n\nYour account is suspended: https://evil.example.com",
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
                "turnstile_token": "solved",
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
            "turnstile_token": "solved",
            "invite_code": invite,
        },
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "invalid_invite"


# ---------------------------------------------------------------------------
# Open signup: Turnstile is the gate, the invite is a comp path (NEU-1343, D-18)
# ---------------------------------------------------------------------------


def _signup_body(email: str = "open@example.com", **overrides) -> dict[str, object]:
    return {
        "email": email,
        "password": "hunter2hunter2",
        "display_name": "Open",
        "turnstile_token": "solved",
        **overrides,
    }


@contextmanager
def _settings_override(**overrides: object) -> Iterator[None]:
    """Run the app against a copy of the settings. Both the route and `deps.get_turnstile`
    read `get_settings`, so one override moves them together."""
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update=overrides)
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_settings, None)


async def _user_exists(session, email: str) -> bool:
    rows = await session.execute(select(User).where(User.email == email))
    return rows.scalar_one_or_none() is not None


async def test_signup_succeeds_with_no_invite_at_all(client, session):
    """The change itself: a solved challenge is the whole of what open signup asks for."""
    r = await client.post("/auth/signup", json=_signup_body())

    assert r.status_code == 201
    assert r.json()["email"] == "open@example.com"
    assert {c.name for c in r.cookies.jar} >= {"upmovies_session", "csrf_token"}


async def test_signup_hands_turnstile_the_token_from_the_body(client, turnstile):
    await client.post("/auth/signup", json=_signup_body(email="scored@example.com"))

    assert turnstile.seen == ["solved"]


async def test_a_failed_challenge_is_refused_and_writes_nothing(client, session, turnstile):
    """403 before the user row, not after: a bot check that ran after the account existed
    would be a bot check that had already lost."""
    turnstile.verdict = False

    r = await client.post("/auth/signup", json=_signup_body(email="bot@example.com"))

    assert r.status_code == 403
    assert r.json()["detail"] == "invalid_turnstile"
    assert not await _user_exists(session, "bot@example.com")


async def test_an_unreachable_turnstile_refuses_the_signup(client, session, turnstile):
    """No verdict is not a pass (`app/turnstile.py`): an outage at Cloudflare closes the door
    rather than opening it, and says so with a 503 the caller can retry."""
    turnstile.failure = TurnstileUnavailable("siteverify is down")

    r = await client.post("/auth/signup", json=_signup_body(email="outage@example.com"))

    assert r.status_code == 503
    assert r.json()["detail"] == "turnstile_unavailable"
    assert not await _user_exists(session, "outage@example.com")


async def test_signup_is_refused_when_no_turnstile_secret_is_configured(client, session):
    """An unset `TURNSTILE_SECRET` is an unguarded door, so the route refuses to be one. The
    autouse stub is dropped here precisely so `deps.get_turnstile` runs for real."""
    app.dependency_overrides.pop(get_turnstile, None)

    with _settings_override(turnstile_secret=""):
        r = await client.post("/auth/signup", json=_signup_body(email="ungated@example.com"))

    assert r.status_code == 503
    assert r.json()["detail"] == "turnstile_unconfigured"
    assert not await _user_exists(session, "ungated@example.com")


async def test_a_supplied_invite_is_still_validated_and_consumed(client, session, make_invite):
    """The comp path survives the change intact: a code that is offered is spent, in the same
    transaction as the user it let in."""
    code = await make_invite()

    r = await client.post(
        "/auth/signup", json=_signup_body(email="comped@example.com", invite_code=code)
    )

    assert r.status_code == 201
    invite = await session.get(Invite, code)
    assert invite is not None
    # The route committed through its own session, so the copy this one holds is stale.
    await session.refresh(invite)
    assert invite.consumed_at is not None
    assert invite.consumed_by_user_id is not None


async def test_a_bad_invite_still_fails_the_signup_rather_than_being_ignored(client, session):
    """Someone typing a code in is telling us they were given one. Quietly opening a plain
    account instead would hide a mistake worth seeing."""
    r = await client.post(
        "/auth/signup", json=_signup_body(email="typo@example.com", invite_code="not-a-code")
    )

    assert r.status_code == 403
    assert r.json()["detail"] == "invalid_invite"
    assert not await _user_exists(session, "typo@example.com")


async def test_signup_open_false_restores_the_invite_requirement(client, session):
    """The rollback switch (D-18): off does not close signup, it puts the invite back in
    front of it."""
    with _settings_override(signup_open=False):
        r = await client.post("/auth/signup", json=_signup_body(email="rolled@example.com"))

    assert r.status_code == 403
    assert r.json()["detail"] == "invalid_invite"
    assert not await _user_exists(session, "rolled@example.com")


async def test_an_invite_still_gets_in_while_signup_is_closed(client, make_invite):
    """The other half of the switch: an admin who can issue invites can still let people in
    while the open door is shut."""
    code = await make_invite()

    with _settings_override(signup_open=False):
        r = await client.post(
            "/auth/signup", json=_signup_body(email="still-in@example.com", invite_code=code)
        )

    assert r.status_code == 201


async def test_the_challenge_is_verified_even_while_signup_is_closed(client, turnstile):
    """Turnstile is not the thing `SIGNUP_OPEN` rolls back — an invite code was never a bot
    check."""
    turnstile.verdict = False

    with _settings_override(signup_open=False):
        r = await client.post(
            "/auth/signup", json=_signup_body(email="closed-bot@example.com", invite_code="x")
        )

    assert r.status_code == 403
    assert r.json()["detail"] == "invalid_turnstile"


@pytest.mark.asyncio
async def test_login_succeeds_with_correct_credentials(client, make_invite):
    invite = await make_invite()
    await client.post(
        "/auth/signup",
        json={
            "email": "lo@example.com",
            "password": "hunter2hunter2",
            "display_name": "Lo",
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
            "turnstile_token": "solved",
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
async def test_signup_route_returns_authed_user_and_sets_cookies(session, make_invite, turnstile):
    invite = await make_invite()
    request = _request()
    response = Response()
    settings = get_settings()
    payload = SignupRequest(
        email="signup@example.com",
        password="hunter2hunter2",
        display_name="Sign",
        turnstile_token="solved",
        invite_code=invite,
    )
    # Called directly rather than through the app, so neither `get_mailer` nor
    # `get_turnstile` is resolved and both have to be handed over by name (NEU-1339,
    # NEU-1343).
    result = await auth_router.signup(
        payload,
        request,
        response,
        db=session,
        settings=settings,
        mailer=MailGateway(settings, transport=NoopTransport()),
        verifier=turnstile,
    )
    assert result.email == "signup@example.com"
    assert result.csrf_token
    set_cookie_headers = response.headers.getlist("set-cookie")
    assert any("upmovies_session=" in h for h in set_cookie_headers)
    assert any("csrf_token=" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_signup_route_raises_409_on_duplicate_email(
    session, make_user, make_invite, turnstile
):
    await make_user(email="dup@example.com")
    invite = await make_invite()
    request = _request()
    response = Response()
    settings = get_settings()
    payload = SignupRequest(
        email="dup@example.com",
        password="hunter2hunter2",
        display_name="Dup",
        turnstile_token="solved",
        invite_code=invite,
    )
    with pytest.raises(HTTPException) as ei:
        await auth_router.signup(
            payload, request, response, db=session, settings=settings, verifier=turnstile
        )
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
