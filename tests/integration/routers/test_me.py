"""Integration tests for me routes."""

import pytest
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from upmovies.app.dto import AccountDeleteRequest
from upmovies.app.models import User
from upmovies.config import get_settings
from upmovies.main import app
from upmovies.routers import me as me_router


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


# ---------------------------------------------------------------------------
# Route-level tests (via ASGITransport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_returns_current_user(authed_client):
    r = await authed_client.get("/me")
    assert r.status_code == 200
    assert r.json()["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_me_includes_is_admin_false_for_regular_user(authed_client):
    r = await authed_client.get("/me")
    assert r.status_code == 200
    assert r.json()["is_admin"] is False


@pytest.mark.asyncio
async def test_me_includes_is_admin_true_for_admin_user(admin_authed_client):
    r = await admin_authed_client.get("/me")
    assert r.status_code == 200
    assert r.json()["is_admin"] is True


@pytest.mark.asyncio
async def test_me_returns_401_when_no_session():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.get("/me")
    assert r.status_code == 401
    assert r.json()["detail"] == "auth_required"


@pytest.mark.asyncio
async def test_delete_me_requires_password(authed_client):
    r = await authed_client.request(
        "DELETE",
        "/me",
        json={"password": "wrong"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_delete_me_succeeds_and_cascades(authed_client, session):
    user_id = authed_client.user.id  # type: ignore[attr-defined]
    r = await authed_client.request(
        "DELETE",
        "/me",
        json={"password": "hunter2hunter2"},
    )
    assert r.status_code == 204
    found = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    assert found is None


@pytest.mark.asyncio
async def test_delete_me_requires_csrf(session, make_user):
    from upmovies.app import tokens
    from upmovies.app.repos import session_repo

    user = await make_user(email="nocsrf@example.com")
    sess_id = tokens.new_session_id()
    await session_repo.create(
        session, session_id=sess_id, user_id=user.id, ttl_days=30, user_agent=None, ip=None
    )
    await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        c.cookies.set("upmovies_session", sess_id, domain="test")
        r = await c.request("DELETE", "/me", json={"password": "hunter2hunter2"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Direct route handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_me_route_raises_401_on_wrong_password(session, make_user):
    user = await make_user(email="dm-rt@example.com", password="hunter2hunter2")
    payload = AccountDeleteRequest(password="wrong")
    with pytest.raises(HTTPException) as ei:
        await me_router.delete_me(payload, user=user, db=session)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_delete_me_route_succeeds_with_correct_password(session, make_user):
    user = await make_user(email="dm-ok-rt@example.com", password="hunter2hunter2")
    payload = AccountDeleteRequest(password="hunter2hunter2")
    out = await me_router.delete_me(payload, user=user, db=session)
    assert out.status_code == 204


@pytest.mark.asyncio
async def test_me_route_returns_authed_user(session, make_user):
    user = await make_user(email="me-rt@example.com")
    settings = get_settings()
    request = _request(cookies={"csrf_token": "ignored-here"})
    out = await me_router.me(request, user=user, settings=settings)
    assert out.email == "me-rt@example.com"
