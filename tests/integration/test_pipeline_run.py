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
from upmovies.ingest.sweep import (
    AdmissionTranches,
    CreditEventResult,
    EnumerateResult,
    FieldEventResult,
    RefreshResult,
)
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


# --- the sweep: four phases, one run row ---------------------------------------


def _stub_phases(monkeypatch, *, enumerated=None, refreshed=None, carded=None, attached=None):
    """Replace all four sweep phases with fakes that record their kwargs and return the
    given results. Returns (calls, captured) — call order and each phase's kwargs."""
    calls: list[str] = []
    captured: dict[str, dict] = {}

    async def fake_enumerate(**kwargs):
        calls.append("enumerate")
        captured["enumerate"] = kwargs
        return enumerated if enumerated is not None else EnumerateResult(seed_people=3)

    async def fake_refresh(**kwargs):
        calls.append("refresh")
        captured["refresh"] = kwargs
        return refreshed if refreshed is not None else RefreshResult(selected=2, refreshed=2)

    async def fake_events(**kwargs):
        calls.append("events")
        captured["events"] = kwargs
        return carded if carded is not None else FieldEventResult(changes_read=1)

    async def fake_credits(**kwargs):
        calls.append("credits")
        captured["credits"] = kwargs
        return attached if attached is not None else CreditEventResult(attachments_read=1)

    monkeypatch.setattr("upmovies.pipeline_run.run_sweep_enumerate", fake_enumerate)
    monkeypatch.setattr("upmovies.pipeline_run.run_sweep_refresh", fake_refresh)
    monkeypatch.setattr("upmovies.pipeline_run.run_field_change_events", fake_events)
    monkeypatch.setattr("upmovies.pipeline_run.run_credit_attachment_events", fake_credits)
    return calls, captured


async def test_sweep_stage_marks_run_failed_on_crash(session, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated sweep crash")

    monkeypatch.setattr("upmovies.pipeline_run.run_sweep_enumerate", boom)
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated sweep crash" in row.error


async def test_sweep_stage_runs_every_phase_and_reports_every_counter(session, monkeypatch):
    """One run row, three phases: the terminal status is the entrypoint's to write, and the
    detail line has to keep all three sets of counters legible on /admin/runs (spec §6.2).
    Order is load-bearing — the events phase reads the `film_field_change` rows refreshing
    has just written, so it can only run last."""
    calls, _ = _stub_phases(
        monkeypatch,
        enumerated=EnumerateResult(seed_people=7, candidates_found=4),
        refreshed=RefreshResult(selected=5, refreshed=5),
        carded=FieldEventResult(changes_read=9, events_created=3, skipped=6),
        attached=CreditEventResult(attachments_read=4, events_created=2, skipped=2),
    )
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, get_settings())

    assert calls == ["enumerate", "refresh", "events", "credits"]
    row = await _run_row(session, run_id)
    assert row.status == "succeeded"
    assert row.error is None
    assert row.detail is not None
    assert "enumerate: 7 seeds, 4 candidates" in row.detail
    assert "refresh: 5/5 refreshed" in row.detail
    assert "events: 3 carded from 9 changes" in row.detail
    assert "credits: 2 carded from 4 attachments" in row.detail


async def test_sweep_stage_passes_the_sweep_settings_to_every_phase(session, monkeypatch):
    _, captured = _stub_phases(monkeypatch)
    settings = get_settings().model_copy(
        update={
            "sweep_dormancy_days": 200,
            "sweep_dormant_refresh_days": 14,
            "sweep_enabled": True,
            "sweep_admit_directors": True,
        }
    )
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, settings)

    enumerate_kwargs = captured["enumerate"]
    assert enumerate_kwargs["dormancy_days"] == 200
    assert enumerate_kwargs["excluded_statuses"] == frozenset({"Released", "Canceled"})
    assert enumerate_kwargs["tranches"] == AdmissionTranches(enabled=True, directors=True)
    refresh_kwargs = captured["refresh"]
    assert refresh_kwargs["dormancy_days"] == 200
    assert refresh_kwargs["dormant_refresh_days"] == 14
    events_kwargs = captured["events"]
    assert events_kwargs["lookback_days"] == settings.sweep_event_lookback_days
    # The two paths that can card one date move share one definition of "the same move".
    assert events_kwargs["corroboration_window_days"] == settings.link_release_change_window_days
    # The credit half reads the same rolling window, for the same reason: re-reading a carded
    # attachment is free, and a watermark would lose what a failed sweep never got to.
    credits_kwargs = captured["credits"]
    assert credits_kwargs["lookback_days"] == settings.sweep_event_lookback_days
    assert credits_kwargs["run_id"] == run_id
    # Every phase shares the run row, and all must guard against the same outage.
    assert enumerate_kwargs["run_id"] == run_id == refresh_kwargs["run_id"]
    assert events_kwargs["run_id"] == run_id
    assert enumerate_kwargs["today"] == refresh_kwargs["today"]


async def test_sweep_stage_refreshes_even_when_enumerate_aborted(session, monkeypatch):
    """The refresh phase is the one the project silently fails without (§6.2), so an
    enumerate that gave up must not take it with it — the cost of trying is bounded by the
    same consecutive-failure guard."""
    calls, _ = _stub_phases(
        monkeypatch,
        enumerated=EnumerateResult(aborted=True, abort_error="aborted after 10 failures"),
    )
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, get_settings())

    assert calls == ["enumerate", "refresh", "events", "credits"]
    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "aborted after 10 failures" in row.error
    assert row.detail and "refresh:" in row.detail


async def test_sweep_stage_fails_the_run_when_the_refresh_phase_aborted(session, monkeypatch):
    _stub_phases(
        monkeypatch,
        refreshed=RefreshResult(aborted=True, abort_error="aborted after 10 failures"),
    )
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "refresh" in row.error


async def test_sweep_stage_fails_the_run_when_the_events_phase_aborted(session, monkeypatch):
    _stub_phases(
        monkeypatch,
        carded=FieldEventResult(aborted=True, abort_error="aborted after 10 failures"),
    )
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "events phase" in row.error


async def test_run_sweep_opens_its_own_run_kind_and_pings(session, monkeypatch):
    """Its own kind, so the sweep gets its own row on /admin/runs rather than hiding inside
    the tmdb stage's counters (spec §6.1)."""
    pings: list[tuple[str | None, str]] = []
    kinds: list[str] = []

    async def fake_stage(run_id, settings, *args, **kwargs):
        async with pipeline_run.SessionLocal() as s:
            kinds.append(
                (await s.execute(select(IngestRun.kind).where(IngestRun.id == run_id))).scalar_one()
            )
            await finalize_run(s, run_id, status="succeeded")
            await s.commit()

    async def fake_ping(base_url, suffix=""):
        pings.append((base_url, suffix))

    monkeypatch.setattr(pipeline_run, "run_sweep_stage", fake_stage)
    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)
    settings = get_settings().model_copy(update={"healthcheck_sweep_url": "https://hc/sweep"})

    ok = await pipeline_run.run_sweep(settings)

    assert ok is True
    assert kinds == ["sweep"]
    assert pings == [("https://hc/sweep", "/start"), ("https://hc/sweep", "")]


async def test_run_sweep_pings_fail_on_failure(session, monkeypatch):
    pings: list[str] = []

    async def fake_stage(run_id, settings, *args, **kwargs):
        async with pipeline_run.SessionLocal() as s:
            await finalize_run(s, run_id, status="failed")
            await s.commit()

    async def fake_ping(base_url, suffix=""):
        pings.append(suffix)

    monkeypatch.setattr(pipeline_run, "run_sweep_stage", fake_stage)
    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)

    ok = await pipeline_run.run_sweep(settings=get_settings())

    assert ok is False
    assert pings == ["/start", "/fail"]


def test_main_runs_the_sweep_arm(monkeypatch):
    """`python -m upmovies.pipeline_run sweep` — its own Coolify slot, ~2h ahead of daily."""
    settings = get_settings().model_copy(update=DEFAULT_ROUTING)
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the sweep arm must not run the daily chain")

    monkeypatch.setattr("upmovies.pipeline_run.run_daily", must_not_run)
    monkeypatch.setattr("upmovies.pipeline_run.run_sweep", _stub_daily(ok=True))
    assert pipeline_run.main(["sweep"]) == 0
    monkeypatch.setattr("upmovies.pipeline_run.run_sweep", _stub_daily(ok=False))
    assert pipeline_run.main(["sweep"]) == 1


async def test_sweep_stage_marks_the_run_failed_when_finalizing_crashes(session, monkeypatch):
    """A stage runner that lets an exception escape leaves the run `running` and skips the
    deadman's /fail, so the finalizing write is inside the same net as the phases."""
    _stub_phases(monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("detail line write failed")

    monkeypatch.setattr("upmovies.pipeline_run._finalize_sweep", boom)
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, get_settings())  # must not raise

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "detail line write failed" in row.error


def test_main_runs_the_sweep_on_an_unroutable_llm_configuration(monkeypatch):
    """The sweep makes no model calls. Failing it on someone else's routing typo would be
    the shared failure mode §6.1 keeps it out of the daily chain to avoid — and it would
    surface only as deadman silence."""
    settings = get_settings().model_copy(
        update={
            **DEFAULT_ROUTING,
            "summary_provider": "deepseek",
            "summary_model": "deepseek-v4-flash",
            "deepseek_api_key": None,
        }
    )
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)
    monkeypatch.setattr("upmovies.pipeline_run.run_sweep", _stub_daily(ok=True))

    assert pipeline_run.main(["sweep"]) == 0


def test_main_rejects_an_unknown_mode(capsys):
    assert pipeline_run.main(["weekly"]) == 2
    assert "sweep" in capsys.readouterr().err


async def test_sweep_stage_fails_the_run_when_the_credit_phase_aborted(session, monkeypatch):
    _stub_phases(
        monkeypatch,
        attached=CreditEventResult(aborted=True, abort_error="aborted after 10 failures"),
    )
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "credits phase" in row.error
