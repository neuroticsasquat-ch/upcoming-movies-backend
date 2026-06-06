"""Admin endpoints for issuing + listing invite codes."""

import pytest
from httpx import ASGITransport, AsyncClient

from upmovies.config import get_settings
from upmovies.main import app


def _admin_header() -> dict[str, str]:
    """Match whatever admin_token the running process's Settings cache has —
    other tests (unit/test_deps.py) clear the @lru_cache mid-run and a stale
    value can stick. Always read via get_settings() so we agree with the
    require_admin dependency."""
    return {"Authorization": f"Bearer {get_settings().admin_token}"}


@pytest.fixture
async def client(session):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_create_invite_requires_admin_token(client):
    r = await client.post("/admin/invites", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_invite_rejects_wrong_token(client):
    r = await client.post(
        "/admin/invites",
        json={},
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_invite_returns_a_code_with_admin_token(client):
    r = await client.post(
        "/admin/invites",
        json={},
        headers=_admin_header(),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["code"]
    assert body["consumed_at"] is None
    assert body["consumed_by_user_id"] is None
    assert body["email_hint"] is None


@pytest.mark.asyncio
async def test_create_invite_with_email_hint(client):
    r = await client.post(
        "/admin/invites",
        json={"email_hint": "alice@example.com"},
        headers=_admin_header(),
    )
    assert r.status_code == 201
    assert r.json()["email_hint"] == "alice@example.com"


@pytest.mark.asyncio
async def test_list_invites_returns_issued_codes(client, make_invite):
    await make_invite()
    await make_invite(email_hint="bob@example.com")
    r = await client.get(
        "/admin/invites",
        headers=_admin_header(),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert any(i["email_hint"] == "bob@example.com" for i in body)


@pytest.mark.asyncio
async def test_list_invites_requires_admin_token(client):
    r = await client.get("/admin/invites")
    assert r.status_code == 401
