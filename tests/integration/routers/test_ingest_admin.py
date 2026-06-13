"""Admin ingest trigger + status endpoints and their background-task wrappers."""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from upmovies.config import get_settings
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run, record_progress
from upmovies.main import app
from upmovies.routers import ingest_admin


def _admin_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().admin_token}"}


@pytest.fixture
async def client(session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


async def _run_row(session, run_id) -> IngestRun:
    return (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


# --- auth gating ---------------------------------------------------------------


async def test_trigger_tmdb_requires_admin_token(client):
    assert (await client.post("/admin/ingest/tmdb")).status_code == 401


async def test_trigger_feeds_requires_admin_token(client):
    assert (await client.post("/admin/ingest/feeds")).status_code == 401


async def test_status_requires_admin_token(client):
    assert (await client.get(f"/admin/ingest/{uuid.uuid4()}")).status_code == 401


# --- triggers create a run + spawn the background task -------------------------


@pytest.fixture
def spy_background(monkeypatch):
    """Swap the background pipeline wrappers for harmless stand-ins so the trigger
    still spawns a real asyncio task, but it doesn't hit the network. (Patching
    asyncio.create_task itself would break the ASGI/DB machinery, which also uses it.)"""
    called: list[tuple[str, uuid.UUID]] = []

    async def fake_tmdb(run_id, settings):
        called.append(("tmdb", run_id))

    async def fake_feeds(run_id, settings):
        called.append(("feeds", run_id))

    monkeypatch.setattr(ingest_admin, "_background_tmdb", fake_tmdb)
    monkeypatch.setattr(ingest_admin, "_background_feeds", fake_feeds)
    return called


async def test_trigger_tmdb_creates_run_and_returns_id(client, session, spy_background):
    r = await client.post("/admin/ingest/tmdb", headers=_admin_header())
    assert r.status_code == 202
    run_id = uuid.UUID(r.json()["run_id"])
    await asyncio.sleep(0.05)  # let the spawned task run
    assert ("tmdb", run_id) in spy_background, "must spawn the tmdb background task"
    row = await _run_row(session, run_id)
    assert row.kind == "tmdb"
    assert row.status == "running"


async def test_trigger_feeds_creates_run_and_returns_id(client, session, spy_background):
    r = await client.post("/admin/ingest/feeds", headers=_admin_header())
    assert r.status_code == 202
    run_id = uuid.UUID(r.json()["run_id"])
    await asyncio.sleep(0.05)
    assert ("feeds", run_id) in spy_background, "must spawn the feeds background task"
    row = await _run_row(session, run_id)
    assert row.kind == "feeds"
    assert row.status == "running"


# --- background wrappers finalize the run -------------------------------------


async def test_background_tmdb_marks_run_failed_on_crash(session, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated tmdb crash")

    monkeypatch.setattr("upmovies.routers.ingest_admin.run_tmdb_ingest", boom)
    run_id = await create_run(session, kind="tmdb")
    await session.commit()

    await ingest_admin._background_tmdb(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated tmdb crash" in row.error


async def test_background_feeds_marks_run_failed_on_crash(session, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated feeds crash")

    monkeypatch.setattr("upmovies.routers.ingest_admin.run_feeds_ingest", boom)
    run_id = await create_run(session, kind="feeds")
    await session.commit()

    await ingest_admin._background_feeds(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated feeds crash" in row.error


# --- end-to-end: trigger path (pipeline mocked) reflected in status ------------


async def test_status_reflects_terminal_state_after_background_run(client, session, monkeypatch):
    from upmovies.ingest.runs import finalize_run

    async def fake_pipeline(*, session_factory, run_id, **kwargs):
        async with session_factory() as s:
            await record_progress(s, run_id, processed_delta=7)
            await finalize_run(s, run_id, status="succeeded")
            await s.commit()

    monkeypatch.setattr("upmovies.routers.ingest_admin.run_feeds_ingest", fake_pipeline)
    run_id = await create_run(session, kind="feeds")
    await session.commit()

    await ingest_admin._background_feeds(run_id, get_settings())

    r = await client.get(f"/admin/ingest/{run_id}", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["kind"] == "feeds"
    assert body["items_processed"] == 7
    assert body["finished_at"] is not None


async def test_status_unknown_run_returns_404(client):
    r = await client.get(f"/admin/ingest/{uuid.uuid4()}", headers=_admin_header())
    assert r.status_code == 404
