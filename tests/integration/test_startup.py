from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.gateway import DEFAULT_ROUTING
from upmovies.config import get_settings
from upmovies.ingest.models import IngestRun
from upmovies.llm import StageConfigurationError
from upmovies.main import app, lifespan, run_startup_cleanup


async def test_startup_cleanup_cancels_stale_running_runs(session):
    stale = IngestRun(
        kind="tmdb",
        status="running",
        started_at=datetime.now(UTC) - timedelta(minutes=60),
    )
    fresh = IngestRun(kind="feeds", status="running", started_at=datetime.now(UTC))
    session.add_all([stale, fresh])
    await session.commit()

    cancelled = await run_startup_cleanup(session, stale_after_minutes=15)
    await session.commit()

    assert cancelled == 1
    rows = {
        r.kind: r.status
        for r in (
            await session.execute(select(IngestRun), execution_options={"populate_existing": True})
        ).scalars()
    }
    assert rows["tmdb"] == "cancelled"
    assert rows["feeds"] == "running"


# --- LLM routing validation at startup (NEU-981, spec §7) -----------------------


async def test_lifespan_validates_the_llm_routing_and_still_starts(session, monkeypatch):
    """The default deploy boots exactly as before, cleanup and all — the check is a guard on
    the way in, not a new reason for a healthy container to fail."""
    settings = get_settings().model_copy(update=DEFAULT_ROUTING)
    monkeypatch.setattr("upmovies.main.get_settings", lambda: settings)
    stale = IngestRun(
        kind="tmdb", status="running", started_at=datetime.now(UTC) - timedelta(minutes=60)
    )
    session.add(stale)
    await session.commit()

    async with lifespan(app):
        pass

    status = (
        await session.execute(
            select(IngestRun.status).where(IngestRun.id == stale.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert status == "cancelled"


async def test_lifespan_refuses_to_start_a_stage_it_cannot_price(monkeypatch):
    """`CLUSTER_PROVIDER` moved, `CLUSTER_MODEL` left behind. Without this the container
    starts, the nightly link run reaches its clustering stage, and `rates_for` raises a bare
    `KeyError` after the run has already committed a few hundred link rows."""
    settings = get_settings().model_copy(
        update={**DEFAULT_ROUTING, "cluster_provider": "deepinfra", "deepinfra_api_key": "di-x"}
    )
    monkeypatch.setattr("upmovies.main.get_settings", lambda: settings)
    with pytest.raises(StageConfigurationError, match="cluster"):
        async with lifespan(app):
            pass


async def test_lifespan_refuses_to_start_a_stage_with_no_credential(monkeypatch):
    """The optional credentials' safety net (design §8): unset is fine until a stage points
    at that provider, and then it fails here rather than mid-run."""
    settings = get_settings().model_copy(
        update={
            **DEFAULT_ROUTING,
            "summary_provider": "deepseek",
            "summary_model": "deepseek-v4-flash",
            "deepseek_api_key": None,
        }
    )
    monkeypatch.setattr("upmovies.main.get_settings", lambda: settings)
    with pytest.raises(StageConfigurationError, match="DEEPSEEK_API_KEY"):
        async with lifespan(app):
            pass
