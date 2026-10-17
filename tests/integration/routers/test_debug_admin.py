"""POST /admin/debug/raise-test-error — the NEU-1590 test-error trigger."""

import pytest
import sentry_sdk
from httpx import ASGITransport, AsyncClient
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from upmovies.config import get_settings
from upmovies.main import app
from upmovies.routers.debug_admin import TelemetryTestError


def _admin_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().admin_token}"}


@pytest.fixture
async def client(session):
    """Depends on `session` so the conftest truncate teardown runs after each test."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
def sentry_events():
    """A live Sentry client whose `before_send` keeps every event and drops it, so nothing
    leaves the process. Closed afterwards so no later test runs with an initialised SDK."""
    events: list[dict] = []

    def keep(event, hint):
        events.append(event)
        return None

    sentry_sdk.init(
        dsn="https://public@bugsink.invalid/1",
        integrations=[StarletteIntegration(), FastApiIntegration()],
        default_integrations=False,
        before_send=keep,
        release="test-sha",
        environment="test",
    )
    try:
        yield events
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.init()


async def test_requires_admin_token(client):
    assert (await client.post("/admin/debug/raise-test-error")).status_code == 401


async def test_raises_its_own_exception_type(client):
    with pytest.raises(TelemetryTestError, match="upmovies.debug.raise-test-error"):
        await client.post("/admin/debug/raise-test-error", headers=_admin_header())


async def test_is_reported_to_sentry_with_release_and_environment(client, sentry_events):
    with pytest.raises(TelemetryTestError):
        await client.post("/admin/debug/raise-test-error", headers=_admin_header())
    [event] = sentry_events
    [exc] = event["exception"]["values"]
    assert exc["type"] == "TelemetryTestError"
    assert event["release"] == "test-sha"
    assert event["environment"] == "test"
