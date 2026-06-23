import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from upmovies.config import get_settings
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
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


async def test_trigger_synthesize_requires_admin_token(client):
    assert (await client.post("/admin/ingest/synthesize")).status_code == 401


async def test_trigger_synthesize_creates_run_and_returns_id(client, session, monkeypatch):
    called: list[tuple[str, uuid.UUID]] = []

    async def fake_synth(run_id, settings):
        called.append(("synthesize", run_id))

    monkeypatch.setattr(ingest_admin, "_background_synth", fake_synth)

    r = await client.post("/admin/ingest/synthesize", headers=_admin_header())
    assert r.status_code == 202
    run_id = uuid.UUID(r.json()["run_id"])
    await asyncio.sleep(0.05)
    assert ("synthesize", run_id) in called
    row = await _run_row(session, run_id)
    assert row.kind == "synthesize"
    assert row.status == "running"


async def test_background_synth_marks_run_failed_on_crash(session, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated synth crash")

    monkeypatch.setattr("upmovies.routers.ingest_admin.run_synthesize_ingest", boom)
    run_id = await create_run(session, kind="synthesize")
    await session.commit()

    await ingest_admin._background_synth(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated synth crash" in row.error
