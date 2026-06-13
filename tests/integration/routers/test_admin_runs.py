"""Session + is_admin protected, read-only ingest-run endpoints for the admin UI.
Distinct from the ADMIN_TOKEN trigger/poll endpoints."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from upmovies.ingest.models import IngestRun
from upmovies.main import app


@pytest.fixture
async def anon_client(session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


async def _seed_runs(session) -> tuple[IngestRun, IngestRun]:
    tmdb = IngestRun(kind="tmdb", status="succeeded", items_processed=5, items_failed=1)
    feeds = IngestRun(kind="feeds", status="running")
    session.add_all([tmdb, feeds])
    await session.commit()
    await session.refresh(tmdb)
    await session.refresh(feeds)
    return tmdb, feeds


# --- gating --------------------------------------------------------------------


async def test_list_runs_requires_authentication(anon_client):
    assert (await anon_client.get("/admin/runs")).status_code == 401


async def test_list_runs_forbidden_for_non_admin(authed_client):
    assert (await authed_client.get("/admin/runs")).status_code == 403


async def test_run_detail_forbidden_for_non_admin(authed_client, session):
    tmdb, _ = await _seed_runs(session)
    assert (await authed_client.get(f"/admin/runs/{tmdb.id}")).status_code == 403


# --- admin reads ---------------------------------------------------------------


async def test_admin_lists_recent_runs(admin_authed_client, session):
    await _seed_runs(session)
    r = await admin_authed_client.get("/admin/runs")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert {row["kind"] for row in body} == {"tmdb", "feeds"}


async def test_admin_run_list_respects_limit(admin_authed_client, session):
    await _seed_runs(session)
    r = await admin_authed_client.get("/admin/runs", params={"limit": 1})
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_admin_gets_run_detail(admin_authed_client, session):
    tmdb, _ = await _seed_runs(session)
    r = await admin_authed_client.get(f"/admin/runs/{tmdb.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(tmdb.id)
    assert body["kind"] == "tmdb"
    assert body["status"] == "succeeded"
    assert body["items_processed"] == 5
    assert body["items_failed"] == 1


async def test_admin_run_detail_unknown_returns_404(admin_authed_client):
    r = await admin_authed_client.get(f"/admin/runs/{uuid.uuid4()}")
    assert r.status_code == 404
