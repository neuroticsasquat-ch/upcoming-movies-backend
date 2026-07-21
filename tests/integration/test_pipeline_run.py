"""In-process ingestion orchestration (upmovies.pipeline_run): the shared stage runners and
the sequential daily/hourly chains driven by the Coolify scheduled tasks."""


import pytest
from sqlalchemy import select

from upmovies import pipeline_run
from upmovies.config import get_settings
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run, finalize_run


async def _run_row(session, run_id) -> IngestRun:
    return (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


# --- stage runners finalize their own run --------------------------------------


async def test_tmdb_stage_marks_run_failed_on_crash(session, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated tmdb crash")

    monkeypatch.setattr("upmovies.pipeline_run.run_tmdb_ingest", boom)
    run_id = await create_run(session, kind="tmdb")
    await session.commit()

    await pipeline_run.run_tmdb_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated tmdb crash" in row.error


async def test_feeds_stage_marks_run_failed_on_crash(session, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated feeds crash")

    monkeypatch.setattr("upmovies.pipeline_run.run_feeds_ingest", boom)
    run_id = await create_run(session, kind="feeds")
    await session.commit()

    await pipeline_run.run_feeds_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated feeds crash" in row.error


async def test_tmdb_stage_passes_excluded_statuses(session, monkeypatch):
    captured: dict = {}

    async def fake(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("upmovies.pipeline_run.run_tmdb_ingest", fake)
    run_id = await create_run(session, kind="tmdb")
    await session.commit()

    await pipeline_run.run_tmdb_stage(run_id, get_settings())

    assert captured["excluded_statuses"] == frozenset({"Released", "Canceled"})


async def test_feeds_stage_passes_per_film_settings(session, monkeypatch):
    captured: dict = {}

    async def fake(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("upmovies.pipeline_run.run_feeds_ingest", fake)
    run_id = await create_run(session, kind="feeds")
    await session.commit()

    await pipeline_run.run_feeds_stage(run_id, get_settings())

    assert captured["per_film_enabled"] is True  # config default
    assert captured["per_film_throttle"] == 1.0


async def test_feeds_stage_per_film_override_wins_over_config(session, monkeypatch):
    captured: dict = {}

    async def fake(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("upmovies.pipeline_run.run_feeds_ingest", fake)
    run_id = await create_run(session, kind="feeds")
    await session.commit()

    await pipeline_run.run_feeds_stage(run_id, get_settings(), per_film_override=False)

    assert captured["per_film_enabled"] is False


async def test_feeds_stage_per_film_override_none_uses_config(session, monkeypatch):
    captured: dict = {}

    async def fake(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("upmovies.pipeline_run.run_feeds_ingest", fake)
    run_id = await create_run(session, kind="feeds")
    await session.commit()

    await pipeline_run.run_feeds_stage(run_id, get_settings(), per_film_override=None)

    assert captured["per_film_enabled"] is True  # config default


# --- orchestration: run_daily / run_hourly -------------------------------------


@pytest.fixture
def spy_stages(monkeypatch):
    """Replace the four stage runners with fakes that record call order and finalize their
    run to a per-stage status (default 'succeeded'). Also captures deadman ping suffixes and
    stubs the network ping. Returns (order, pings, set_status)."""
    order: list[str] = []
    pings: list[str] = []
    status_by_kind: dict[str, str] = {}

    def _make(kind: str):
        async def fake(run_id, settings, *args, **kwargs):
            order.append(kind)
            async with pipeline_run.SessionLocal() as s:
                await finalize_run(s, run_id, status=status_by_kind.get(kind, "succeeded"))
                await s.commit()

        return fake

    for kind in ("tmdb", "feeds", "link", "synthesize"):
        monkeypatch.setattr(pipeline_run, f"run_{kind}_stage", _make(kind))

    async def fake_ping(base_url, suffix=""):
        pings.append(suffix)

    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)
    return order, pings, status_by_kind


async def test_run_daily_runs_all_stages_in_order(session, spy_stages):
    order, pings, _ = spy_stages

    ok = await pipeline_run.run_daily(get_settings())

    assert ok is True
    assert order == ["tmdb", "feeds", "link", "synthesize"]
    assert pings == ["/start", ""], "start ping then success (base URL) ping"


async def test_run_daily_fails_fast_on_stage_failure(session, spy_stages):
    order, pings, status_by_kind = spy_stages
    status_by_kind["link"] = "failed"

    ok = await pipeline_run.run_daily(get_settings())

    assert ok is False
    # link failed → synthesize must never run.
    assert order == ["tmdb", "feeds", "link"]
    assert "synthesize" not in order
    assert pings == ["/start", "/fail"]


async def test_run_daily_synthesize_waits_for_link(session, monkeypatch):
    """Sequential await: synthesize's runner cannot begin until link's has returned."""
    events: list[str] = []

    def _make(kind: str):
        async def fake(run_id, settings, *args, **kwargs):
            events.append(f"{kind}:start")
            events.append(f"{kind}:end")
            async with pipeline_run.SessionLocal() as s:
                await finalize_run(s, run_id, status="succeeded")
                await s.commit()

        return fake

    for kind in ("tmdb", "feeds", "link", "synthesize"):
        monkeypatch.setattr(pipeline_run, f"run_{kind}_stage", _make(kind))
    monkeypatch.setattr(pipeline_run, "_ping", lambda *a, **k: _noop())

    await pipeline_run.run_daily(get_settings())

    assert events.index("link:end") < events.index("synthesize:start")


async def _noop() -> None:
    return None


async def test_run_hourly_runs_feeds_per_film_false(session, monkeypatch):
    captured: dict = {}
    pings: list[str] = []

    async def fake_feeds(run_id, settings, per_film_override=None):
        captured["per_film_override"] = per_film_override
        async with pipeline_run.SessionLocal() as s:
            await finalize_run(s, run_id, status="succeeded")
            await s.commit()

    async def fake_ping(base_url, suffix=""):
        pings.append(suffix)

    monkeypatch.setattr(pipeline_run, "run_feeds_stage", fake_feeds)
    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)

    ok = await pipeline_run.run_hourly(get_settings())

    assert ok is True
    assert captured["per_film_override"] is False
    assert pings == ["/start", ""]


async def test_run_hourly_pings_fail_on_failure(session, monkeypatch):
    pings: list[str] = []

    async def fake_feeds(run_id, settings, per_film_override=None):
        async with pipeline_run.SessionLocal() as s:
            await finalize_run(s, run_id, status="failed")
            await s.commit()

    async def fake_ping(base_url, suffix=""):
        pings.append(suffix)

    monkeypatch.setattr(pipeline_run, "run_feeds_stage", fake_feeds)
    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)

    ok = await pipeline_run.run_hourly(get_settings())

    assert ok is False
    assert pings == ["/start", "/fail"]


# --- deadman ping is best-effort -----------------------------------------------


async def test_ping_noop_when_url_unset(monkeypatch):
    def exploding_client(*args, **kwargs):
        raise AssertionError("must not construct an HTTP client when URL is unset")

    monkeypatch.setattr("upmovies.pipeline_run.httpx.AsyncClient", exploding_client)
    await pipeline_run._ping(None, "/start")  # no exception → no HTTP attempted


async def test_ping_swallows_network_errors(monkeypatch):
    class BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("upmovies.pipeline_run.httpx.AsyncClient", BoomClient)
    # Must not raise despite the POST failing.
    await pipeline_run._ping("https://hc.example/abc", "/fail")
