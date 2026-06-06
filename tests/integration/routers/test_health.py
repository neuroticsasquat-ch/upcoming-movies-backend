"""Integration tests for health routes.

Merges tests from tests/test_health.py and the readyz tests from
tests/test_lifespan_and_health.py.
"""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

from upmovies.deps import get_session
from upmovies.main import app

client = TestClient(app)


def test_healthz_returns_200_and_ok_body():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_returns_200_when_db_reachable(session):
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readyz_returns_503_when_db_unreachable():
    async def broken_session() -> AsyncIterator[object]:
        class _Broken:
            async def execute(self, *_a, **_kw):
                raise RuntimeError("simulated db outage")

        yield _Broken()

    app.dependency_overrides[get_session] = broken_session
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/readyz")
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert r.status_code == 503
    assert "database not reachable" in r.json()["detail"]


@pytest.mark.asyncio
async def test_readyz_returns_ok_when_db_responds(session):
    """Direct call: bypasses ASGITransport so coverage traces the success path."""
    from upmovies.routers.health import readyz

    result = await readyz(session=session)
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_returns_503_when_db_select_fails(monkeypatch):
    """Force the readiness check to fail by patching the session executor
    to raise an OperationalError. Covers routers/health.py:26."""
    from sqlalchemy.exc import OperationalError

    async def _broken_execute(*args, **kwargs):
        raise OperationalError("SELECT 1", None, Exception("pg down"))

    # Patch the SessionLocal context manager so any session yielded raises.
    with patch("upmovies.routers.health.get_session") as fake_dep:
        fake_session = AsyncMock()
        fake_session.execute.side_effect = _broken_execute

        async def _yield_broken():
            yield fake_session

        fake_dep.side_effect = _yield_broken
        # Override the FastAPI dependency directly via app.dependency_overrides.
        from upmovies.deps import get_session as _get_session

        async def _override():
            yield fake_session

        app.dependency_overrides[_get_session] = _override
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url="https://test",
            ) as http_client:
                r = await http_client.get("/readyz")
            assert r.status_code == 503
            assert "database not reachable" in r.json()["detail"]
        finally:
            app.dependency_overrides.pop(_get_session, None)
