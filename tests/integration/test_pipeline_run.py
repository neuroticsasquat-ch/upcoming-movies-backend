"""In-process ingestion orchestration (upmovies.pipeline_run): the shared stage runners and
the sequential daily/hourly chains driven by the Coolify scheduled tasks."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tests.fixtures.gateway import DEFAULT_ROUTING, StubGateway
from upmovies import pipeline_run
from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run, finalize_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm import StageConfigurationError
from upmovies.news.models import Story


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


async def test_link_stage_marks_run_failed_on_crash(session, monkeypatch):
    """A link run that crashes must be finalized `failed`, so run_daily aborts before
    synthesize instead of summarizing unlinked stories."""

    async def boom(**kwargs):
        raise RuntimeError("simulated link crash")

    monkeypatch.setattr("upmovies.pipeline_run.run_link_ingest", boom)
    run_id = await create_run(session, kind="link")
    await session.commit()

    await pipeline_run.run_link_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated link crash" in row.error


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


async def test_run_daily_aborts_when_link_stage_totally_fails(
    session, session_factory, monkeypatch
):
    """NEU-986, end to end through the real link pipeline: a total LLM outage fails every
    chunk, so the run finalizes `failed` off its counters alone (no crash propagates out of
    the stage). That status is what makes the chain stop before `synthesize` and ping the
    deadman `/fail` — the incident NEU-743 fixed, where it pinged green instead."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add(
        Story(
            source="X",
            url="https://e/outage",
            title="Runner news",
            published_at=datetime.now(UTC),
            link_status="pending",
            raw={"summary": ""},
        )
    )
    await session.commit()

    class _OutageClient:
        async def complete_call(self, **kwargs):
            raise RuntimeError("total outage")

    order: list[str] = []
    pings: list[str] = []

    def _make(kind: str):
        async def fake(run_id, settings, *args, **kwargs):
            order.append(kind)
            async with pipeline_run.SessionLocal() as s:
                await finalize_run(s, run_id, status="succeeded")
                await s.commit()

        return fake

    for kind in ("tmdb", "feeds", "synthesize"):
        monkeypatch.setattr(pipeline_run, f"run_{kind}_stage", _make(kind))

    async def real_link_stage(run_id, settings, *args, **kwargs):
        order.append("link")
        await run_link_ingest(
            session_factory=session_factory,
            gateway=StubGateway(_OutageClient()),
            run_id=run_id,
            model="claude-haiku-4-5",
            cluster_model="claude-sonnet-4-6",
            recency_days=45,
            batch_size=10,
            floor=0.7,
        )

    monkeypatch.setattr(pipeline_run, "run_link_stage", real_link_stage)

    async def fake_ping(base_url, suffix=""):
        pings.append(suffix)

    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)

    ok = await pipeline_run.run_daily(get_settings())

    assert ok is False
    assert order == ["tmdb", "feeds", "link"]  # synthesize never ran
    assert pings == ["/start", "/fail"]


async def test_run_daily_continues_past_a_lone_cluster_failure(
    session, session_factory, monkeypatch
):
    """NEU-987, the counterpart to the test above: clustering is self-healing, so one
    pathological film on an otherwise empty backlog must NOT abort the chain. Before the
    denominator, `cluster` reporting 0 processed / 1 failed failed the link run, and because
    `run_daily` is fail-fast that meant `synthesize` never ran and the deadman got `/fail`
    every day for as long as the film stayed unclusterable."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add(
        Story(
            source="X",
            url="https://e/linked-unclustered",
            title="Runner news",
            published_at=datetime.now(UTC),
            link_status="linked",  # nothing pending → the link stage is a legitimate no-op
            film_id=film.id,
            raw={"summary": ""},
        )
    )
    await session.commit()

    class _OutageClient:
        async def complete_call(self, **kwargs):
            raise RuntimeError("this one film never clusters")

    order: list[str] = []
    pings: list[str] = []

    def _make(kind: str):
        async def fake(run_id, settings, *args, **kwargs):
            order.append(kind)
            async with pipeline_run.SessionLocal() as s:
                await finalize_run(s, run_id, status="succeeded")
                await s.commit()

        return fake

    for kind in ("tmdb", "feeds", "synthesize"):
        monkeypatch.setattr(pipeline_run, f"run_{kind}_stage", _make(kind))

    async def real_link_stage(run_id, settings, *args, **kwargs):
        order.append("link")
        await run_link_ingest(
            session_factory=session_factory,
            gateway=StubGateway(_OutageClient()),
            run_id=run_id,
            model="claude-haiku-4-5",
            cluster_model="claude-sonnet-4-6",
            recency_days=45,
            batch_size=10,
            floor=0.7,
        )

    monkeypatch.setattr(pipeline_run, "run_link_stage", real_link_stage)

    async def fake_ping(base_url, suffix=""):
        pings.append(suffix)

    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)

    ok = await pipeline_run.run_daily(get_settings())

    assert ok is True
    assert order == ["tmdb", "feeds", "link", "synthesize"]  # the chain ran to completion
    assert pings == ["/start", ""]  # green, not /fail


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


# --- the scheduled task refuses an unroutable stage before it starts ------------


def _stub_daily(*, ok: bool):
    async def _run(settings):
        return ok

    return _run


def test_main_validates_the_llm_routing_before_running_anything(monkeypatch):
    """A scheduled task is its own process, so the app's lifespan check does not cover it —
    and this is the process the mid-publish `KeyError` was costing (NEU-981, spec §7). It
    must refuse before the chain opens its first run, not partway through it."""
    settings = get_settings().model_copy(
        update={
            **DEFAULT_ROUTING,
            "summary_provider": "deepseek",
            "summary_model": "deepseek-v4-flash",
            "deepseek_api_key": None,
        }
    )
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the daily chain must not start on an unroutable stage")

    monkeypatch.setattr("upmovies.pipeline_run.run_daily", must_not_run)
    with pytest.raises(StageConfigurationError, match="DEEPSEEK_API_KEY"):
        pipeline_run.main(["daily"])


def test_main_runs_the_chain_when_the_routing_is_sound(monkeypatch):
    """The guard is a gate, not a wall: the default all-Anthropic routing still reaches the
    chain, and `main` still reports its outcome as the exit code."""
    settings = get_settings().model_copy(update=DEFAULT_ROUTING)
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)
    monkeypatch.setattr("upmovies.pipeline_run.run_daily", _stub_daily(ok=True))
    assert pipeline_run.main(["daily"]) == 0
    monkeypatch.setattr("upmovies.pipeline_run.run_daily", _stub_daily(ok=False))
    assert pipeline_run.main(["daily"]) == 1
