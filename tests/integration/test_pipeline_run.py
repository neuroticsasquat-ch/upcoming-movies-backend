"""In-process ingestion orchestration (upmovies.pipeline_run): the shared stage runners and
the sequential daily/hourly chains driven by the Coolify scheduled tasks."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.gateway import DEFAULT_ROUTING, StubGateway
from upmovies import pipeline_run
from upmovies.app.models import PushSubscription
from upmovies.app.services.alert_sender import AlertSendResult
from upmovies.app.services.digest_sender import DigestSendResult
from upmovies.app.services.notify_service import NotifyResult
from upmovies.app.services.push_sender import PushSendResult
from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.ingest.models import IngestRun
from upmovies.ingest.providers import ProvidersResult
from upmovies.ingest.runs import create_run, finalize_run
from upmovies.ingest.sweep import (
    AdmissionTranches,
    CreditDetachmentResult,
    CreditEventResult,
    EnumerateResult,
    FieldEventResult,
    RefreshResult,
    ReleaseEventResult,
)
from upmovies.ingest.videos import VideosResult
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm import StageConfigurationError
from upmovies.mail import MailConfigurationError
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


async def test_run_daily_clears_an_orphaned_run_before_opening_its_own(session, spy_stages):
    """The stale-run canceller used to run only in the app's lifespan — i.e. on deploy — so
    a run orphaned by a killed scheduled task sat `running` until the next release. Every
    scheduled task now sweeps first (NEU-1117)."""
    orphan = IngestRun(
        kind="sweep",
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=6),
    )
    session.add(orphan)
    await session.commit()

    await pipeline_run.run_daily(get_settings())

    row = await _run_row(session, orphan.id)
    assert row.status == "cancelled"


async def test_run_daily_leaves_a_heartbeating_run_alone(session, spy_stages):
    """The cleanup is safe to run from a task that may overlap a live sweep precisely
    because expiry is heartbeat-based."""
    alive = IngestRun(
        kind="sweep",
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=6),
        last_progress_at=datetime.now(UTC),
    )
    session.add(alive)
    await session.commit()

    await pipeline_run.run_daily(get_settings())

    assert (await _run_row(session, alive.id)).status == "running"


async def test_run_hourly_clears_an_orphaned_run_before_opening_its_own(session, monkeypatch):
    """The mode that makes the cleanup worth having: hourly is the most frequent slot, so it
    is what bounds an orphan's life to an hour rather than to the next deploy (NEU-1117)."""
    orphan = IngestRun(
        kind="sweep",
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=6),
    )
    session.add(orphan)
    await session.commit()

    async def fake_feeds(run_id, settings, *args, **kwargs):
        async with pipeline_run.SessionLocal() as s:
            await finalize_run(s, run_id, status="succeeded")
            await s.commit()

    monkeypatch.setattr(pipeline_run, "run_feeds_stage", fake_feeds)
    monkeypatch.setattr(pipeline_run, "_ping", lambda *a, **k: _noop())

    await pipeline_run.run_hourly(get_settings())

    assert (await _run_row(session, orphan.id)).status == "cancelled"


async def test_run_sweep_clears_an_orphaned_run_before_opening_its_own(session, monkeypatch):
    orphan = IngestRun(
        kind="sweep",
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=6),
    )
    session.add(orphan)
    await session.commit()

    async def fake_sweep(run_id, settings):
        async with pipeline_run.SessionLocal() as s:
            await finalize_run(s, run_id, status="succeeded")
            await s.commit()

    monkeypatch.setattr(pipeline_run, "run_sweep_stage", fake_sweep)
    monkeypatch.setattr(pipeline_run, "_ping", lambda *a, **k: _noop())

    await pipeline_run.run_sweep(get_settings())

    assert (await _run_row(session, orphan.id)).status == "cancelled"


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


# --- the sweep: every phase, one run row ---------------------------------------


def _stub_phases(
    monkeypatch,
    *,
    enumerated=None,
    refreshed=None,
    carded=None,
    attached=None,
    detached=None,
    released=None,
):
    """Replace every sweep phase with a fake that records its kwargs and returns the
    given result. Returns (calls, captured) — call order and each phase's kwargs."""
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

    async def fake_detachments(**kwargs):
        calls.append("credit removals")
        captured["credit removals"] = kwargs
        return detached if detached is not None else CreditDetachmentResult(detachments_read=1)

    async def fake_release(**kwargs):
        calls.append("release dates")
        captured["release dates"] = kwargs
        return released if released is not None else ReleaseEventResult(changes_read=1)

    monkeypatch.setattr("upmovies.pipeline_run.run_sweep_enumerate", fake_enumerate)
    monkeypatch.setattr("upmovies.pipeline_run.run_sweep_refresh", fake_refresh)
    monkeypatch.setattr("upmovies.pipeline_run.run_field_change_events", fake_events)
    monkeypatch.setattr("upmovies.pipeline_run.run_credit_attachment_events", fake_credits)
    monkeypatch.setattr("upmovies.pipeline_run.run_credit_detachment_events", fake_detachments)
    monkeypatch.setattr("upmovies.pipeline_run.run_release_date_events", fake_release)
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
        released=ReleaseEventResult(changes_read=11, events_created=3, skipped=8),
    )
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, get_settings())

    assert calls == [
        "enumerate",
        "refresh",
        "events",
        "credits",
        "credit removals",
        "release dates",
    ]
    row = await _run_row(session, run_id)
    assert row.status == "succeeded"
    assert row.error is None
    assert row.detail is not None
    assert "enumerate: 7 seeds (0 missing), 4 candidates" in row.detail
    assert "refresh: 5/5 refreshed" in row.detail
    assert "events: 3 carded from 9 changes" in row.detail
    assert "credits: 2 carded from 4 attachments" in row.detail
    assert "credit removals: 0 carded from 1 detachments" in row.detail
    assert "release dates: 3 carded from 11 changes" in row.detail
    # No watchlist clause: the derivation phase went with the table it maintained (M8).
    assert "watchlist" not in row.detail


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
    # The two paths that can card one date move share one definition of "the same move" — a
    # rule that moved to the release-date phase with the dates themselves (NEU-1121).
    release_kwargs = captured["release dates"]
    assert release_kwargs["lookback_days"] == settings.sweep_event_lookback_days
    assert release_kwargs["corroboration_window_days"] == settings.link_release_change_window_days
    assert release_kwargs["run_id"] == run_id
    # The credit half reads the same rolling window, for the same reason: re-reading a carded
    # attachment is free, and a watermark would lose what a failed sweep never got to.
    credits_kwargs = captured["credits"]
    assert credits_kwargs["lookback_days"] == settings.sweep_event_lookback_days
    assert credits_kwargs["run_id"] == run_id
    # Every phase shares the run row, and all must guard against the same outage.
    assert enumerate_kwargs["run_id"] == run_id == refresh_kwargs["run_id"]
    assert events_kwargs["run_id"] == run_id
    assert enumerate_kwargs["today"] == refresh_kwargs["today"]


async def test_sweep_threads_dwell_days(session, monkeypatch):
    """NEU-1205: the credit detachment phase receives the configured dwell-days value."""
    _, captured = _stub_phases(monkeypatch)
    settings = get_settings().model_copy(update={"sweep_credit_dwell_days": 3})
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, settings)

    assert captured["credit removals"]["dwell_days"] == 3


async def test_sweep_threads_quarantine_hours(session, monkeypatch):
    """NEU-1368: the credit attachment phase receives the configured quarantine window. The
    runner defaults it to 0 — no hold — so a value that never arrives is invisible."""
    _, captured = _stub_phases(monkeypatch)
    settings = get_settings().model_copy(update={"sweep_credit_quarantine_hours": 72})
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, settings)

    assert captured["credits"]["quarantine_hours"] == 72


async def test_sweep_threads_the_sanity_thresholds_and_a_client(session, monkeypatch):
    """NEU-1370: all three thresholds reach the credit phase, *and* a TMDB client does — the
    two date checks are inert without one, so a client that never arrives would leave two
    thirds of D-8 silently switched off."""
    _, captured = _stub_phases(monkeypatch)
    settings = get_settings().model_copy(
        update={
            "sweep_sanity_max_films_per_day": 20,
            "sweep_sanity_posthumous_years": 2,
            "sweep_sanity_min_age_years": 3,
        }
    )
    run_id = await create_run(session, kind="sweep")
    await session.commit()

    await pipeline_run.run_sweep_stage(run_id, settings)

    credits = captured["credits"]
    assert credits["max_films_per_day"] == 20
    assert credits["posthumous_years"] == 2
    assert credits["min_age_years"] == 3
    assert credits["client"] is not None


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

    assert calls == [
        "enumerate",
        "refresh",
        "events",
        "credits",
        "credit removals",
        "release dates",
    ]
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


def test_main_validates_the_mail_configuration_before_running_anything(monkeypatch):
    """A scheduled task is its own process and holds the env it was created with, so the app
    having booted proves nothing about this one. No mode sends mail today; the guard is here
    for M7's notify and digest passes, which are `pipeline_run` modes."""
    settings = get_settings().model_copy(
        update={**DEFAULT_ROUTING, "mail_provider": "resend", "resend_api_key": None}
    )
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the daily chain must not start on an unusable mail configuration")

    monkeypatch.setattr("upmovies.pipeline_run.run_daily", must_not_run)
    with pytest.raises(MailConfigurationError, match="RESEND_API_KEY"):
        pipeline_run.main(["daily"])


def test_main_runs_the_sweep_on_an_unusable_mail_configuration(monkeypatch):
    """The sweep inherits the LLM guard's exemption for the same reason it has one: it is
    deliberately outside the daily chain's shared failure modes (§6.1), and it sends no mail.
    """
    settings = get_settings().model_copy(
        update={**DEFAULT_ROUTING, "mail_provider": "resend", "resend_api_key": None}
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


# --- the watch-provider poll (D-27) and the video poll (D-35) ------------------


def _stub_poll(
    monkeypatch,
    result: ProvidersResult | None = None,
    videos: VideosResult | None = None,
) -> dict:
    """Replace both phases with fakes that record their kwargs and return the given results."""
    captured: dict = {}

    async def fake_poll(**kwargs):
        captured.update(kwargs)
        return result if result is not None else ProvidersResult(selected=2, polled=2)

    async def fake_videos(**kwargs):
        captured.update({f"videos_{k}": v for k, v in kwargs.items()})
        return videos if videos is not None else VideosResult(selected=2, polled=2)

    monkeypatch.setattr("upmovies.pipeline_run.run_provider_poll", fake_poll)
    monkeypatch.setattr("upmovies.pipeline_run.run_video_poll", fake_videos)
    return captured


async def test_providers_stage_finalizes_the_run_with_both_detail_lines(session, monkeypatch):
    """Neither phase finalizes — the status, the error and the detail line belong to whoever
    opened the run, the same division of labour the sweep uses. Both clauses are reported so a
    green providers pass cannot hide a video pass that gave up."""
    _stub_poll(
        monkeypatch,
        ProvidersResult(selected=9, polled=8, offers=31, first_seen=2, cards=1, missing=1),
        VideosResult(selected=9, polled=9, videos=22, recorded=3, baselined=1, cards=2),
    )
    run_id = await create_run(session, kind="providers")
    await session.commit()

    await pipeline_run.run_providers_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "succeeded"
    assert row.error is None
    assert row.detail == (
        "providers: 8/9 polled, 31 offers, 2 first seen, 1 carded, 1 missing, 0 failed; "
        "videos: 9/9 polled, 22 videos, 3 recorded, 1 baselined, 2 carded, 0 missing, 0 failed"
    )


async def test_providers_stage_passes_the_poll_window_from_settings(session, monkeypatch):
    captured = _stub_poll(monkeypatch)
    settings = get_settings().model_copy(
        update={"provider_poll_min_age_days": 7, "provider_poll_max_age_days": 120}
    )
    run_id = await create_run(session, kind="providers")
    await session.commit()

    await pipeline_run.run_providers_stage(run_id, settings)

    assert (captured["min_age_days"], captured["max_age_days"]) == (7, 120)


async def test_both_phases_select_from_the_same_window_and_the_same_day(session, monkeypatch):
    """One working set, read twice. A run straddling midnight must not poll providers over
    yesterday's films and videos over today's and report them as one pass."""
    captured = _stub_poll(monkeypatch)
    settings = get_settings().model_copy(
        update={"provider_poll_min_age_days": 7, "provider_poll_max_age_days": 120}
    )
    run_id = await create_run(session, kind="providers")
    await session.commit()

    await pipeline_run.run_providers_stage(run_id, settings)

    assert captured["videos_today"] == captured["today"]
    assert (captured["videos_min_age_days"], captured["videos_max_age_days"]) == (7, 120)
    assert captured["videos_run_id"] == captured["run_id"] == run_id


async def test_providers_stage_fails_the_run_when_the_poll_aborted(session, monkeypatch):
    _stub_poll(
        monkeypatch,
        ProvidersResult(selected=9, polled=3, aborted=True, abort_error="aborted after 10"),
    )
    run_id = await create_run(session, kind="providers")
    await session.commit()

    await pipeline_run.run_providers_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error == "aborted after 10"


async def test_providers_stage_fails_the_run_when_the_video_poll_aborted(session, monkeypatch):
    """The video pass is the second half of the same slot, and its deadman is the run's — a
    poll that gave up has to turn the check red rather than ride a green providers pass."""
    _stub_poll(
        monkeypatch,
        videos=VideosResult(selected=9, polled=3, aborted=True, abort_error="videos gave up"),
    )
    run_id = await create_run(session, kind="providers")
    await session.commit()

    await pipeline_run.run_providers_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error == "videos gave up"
    assert row.detail and "videos aborted: videos gave up" in row.detail


async def test_providers_stage_marks_run_failed_on_crash(session, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated provider poll crash")

    monkeypatch.setattr("upmovies.pipeline_run.run_provider_poll", boom)
    run_id = await create_run(session, kind="providers")
    await session.commit()

    await pipeline_run.run_providers_stage(run_id, get_settings())  # must not raise

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated provider poll crash" in row.error


async def test_run_providers_opens_its_own_run_kind_and_pings_its_own_deadman(session, monkeypatch):
    """Its own kind and its own check: a poll that stopped running is invisible in every
    deadman that already exists, because it is a stage in no other chain."""
    kinds: list[str] = []
    pings: list[tuple[str | None, str]] = []

    async def fake_stage(run_id, settings, *args, **kwargs):
        async with pipeline_run.SessionLocal() as s:
            kinds.append(
                (await s.execute(select(IngestRun.kind).where(IngestRun.id == run_id))).scalar_one()
            )
            await finalize_run(s, run_id, status="succeeded")
            await s.commit()

    async def fake_ping(base_url, suffix=""):
        pings.append((base_url, suffix))

    monkeypatch.setattr(pipeline_run, "run_providers_stage", fake_stage)
    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)
    settings = get_settings().model_copy(
        update={"healthcheck_providers_url": "https://hc/providers"}
    )

    ok = await pipeline_run.run_providers(settings)

    assert ok is True
    assert kinds == ["providers"]
    assert pings == [
        ("https://hc/providers", "/start"),
        ("https://hc/providers", ""),
    ]


def test_main_runs_the_providers_arm(monkeypatch):
    """`python -m upmovies.pipeline_run providers` — the fourth Coolify slot (D-27)."""
    settings = get_settings().model_copy(update=DEFAULT_ROUTING)
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the providers arm must not run the daily chain")

    monkeypatch.setattr("upmovies.pipeline_run.run_daily", must_not_run)
    monkeypatch.setattr("upmovies.pipeline_run.run_providers", _stub_daily(ok=True))
    assert pipeline_run.main(["providers"]) == 0
    monkeypatch.setattr("upmovies.pipeline_run.run_providers", _stub_daily(ok=False))
    assert pipeline_run.main(["providers"]) == 1


def test_main_runs_the_providers_arm_on_an_unroutable_llm_configuration(monkeypatch):
    """The poll is TMDB reads and catalog writes end to end, so it joins the sweep in the
    exemption: failing it on someone else's routing typo would surface only as deadman
    silence on a slot that reaches no model."""
    settings = get_settings().model_copy(
        update={
            **DEFAULT_ROUTING,
            "summary_provider": "deepseek",
            "summary_model": "deepseek-v4-flash",
            "deepseek_api_key": None,
            "mail_provider": "resend",
            "resend_api_key": None,
        }
    )
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)
    monkeypatch.setattr("upmovies.pipeline_run.run_providers", _stub_daily(ok=True))

    assert pipeline_run.main(["providers"]) == 0


# --- notify: M7's decision pass on the fifth slot (NEU-1379, D-31) ---


def _stub_notify(monkeypatch, result: NotifyResult | None = None) -> dict:
    """Replace the pass itself. What `run_notify_stage` owes is the run row, not the decisions
    — those are `tests/integration/app/test_notify_pass.py`'s subject."""
    captured: dict = {}

    async def fake_pass(**kwargs):
        captured.update(kwargs)
        return result if result is not None else NotifyResult()

    monkeypatch.setattr("upmovies.pipeline_run.run_notify_pass", fake_pass)
    return captured


def _stub_alert_send(monkeypatch, result: AlertSendResult | None = None) -> dict:
    """Replace the send pass. Same division as `_stub_notify`: this file owns the run row."""
    captured: dict = {}

    async def fake_send(**kwargs):
        captured.update(kwargs)
        return result if result is not None else AlertSendResult()

    monkeypatch.setattr("upmovies.pipeline_run.send_queued_alerts", fake_send)
    return captured


def _stub_push_send(monkeypatch, result: PushSendResult | None = None) -> dict:
    """Replace the push half of the send (NEU-1387). Same division again: what the
    notifications say is `tests/integration/app/test_push_sender.py`'s subject."""
    captured: dict = {}

    async def fake_push(**kwargs):
        captured.update(kwargs)
        return result if result is not None else PushSendResult()

    monkeypatch.setattr("upmovies.pipeline_run.send_queued_pushes", fake_push)
    return captured


async def test_notify_stage_finalizes_the_run_with_its_detail_line(session, monkeypatch):
    _stub_notify(
        monkeypatch,
        NotifyResult(users_considered=3, events_considered=7, alerts_queued=2, digests_queued=4),
    )
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "succeeded"
    assert row.error is None
    assert row.detail == (
        "notify: 7 events, 3 users, 2 alerts, 0 push, 4 digests, 0 suppressed, 0 failed; "
        "alerts: 0 mails to 0 users, 0 sent, 0 failed, 0 suppressed, 0 lost; "
        "push: 0 notifications to 0 users, 0 delivered, 0 failed, 0 suppressed, 0 pruned, 0 lost"
    )


async def test_notify_stage_fails_the_run_when_the_pass_aborted(session, monkeypatch):
    _stub_notify(
        monkeypatch, NotifyResult(failures=10, aborted=True, abort_error="aborted after 10")
    )
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error == "aborted after 10"


async def test_notify_stage_sends_the_alerts_the_decision_pass_queued(session, monkeypatch):
    """The two phases under one run row (NEU-1380). What is asserted here is the *sequencing* —
    that the send pass runs, with the run id and a gateway — not what it mails, which is
    `tests/integration/app/test_alert_sender.py`'s subject."""
    _stub_notify(monkeypatch, NotifyResult(alerts_queued=2))
    captured = _stub_alert_send(
        monkeypatch, AlertSendResult(users_considered=1, mails_sent=1, sent=2)
    )
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    assert captured["run_id"] == run_id
    assert captured["mailer"] is not None
    row = await _run_row(session, run_id)
    assert row.status == "succeeded"
    assert row.detail is not None
    assert "alerts: 1 mails to 1 users, 2 sent, 0 failed, 0 suppressed, 0 lost" in row.detail


async def test_notify_stage_sends_nothing_when_the_decision_pass_aborted(session, monkeypatch):
    """Consecutive failures deciding is not a state to start mailing out of; the rows already
    written stay `queued` for the next run."""
    _stub_notify(monkeypatch, NotifyResult(aborted=True, abort_error="aborted after 10"))

    async def must_not_run(**kwargs):
        raise AssertionError("an aborted decision pass must not reach the sender")

    monkeypatch.setattr("upmovies.pipeline_run.send_queued_alerts", must_not_run)
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"


async def test_notify_stage_fails_the_run_when_the_send_pass_aborted(session, monkeypatch):
    """A mail provider that is down fails the run — `send_queued_alerts` aborts on consecutive
    provider refusals — so the watermark does not advance past a window whose alerts never went
    out, and the deadman says so. That the refusals really do reach the guard is
    `tests/integration/app/test_alert_sender.py`'s subject; this asserts the run row."""
    _stub_notify(monkeypatch)
    _stub_alert_send(
        monkeypatch, AlertSendResult(aborted=True, abort_error="alert send aborted after 10")
    )
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error == "alert send aborted after 10"


async def test_notify_stage_pushes_the_alerts_the_decision_pass_queued(session, monkeypatch):
    """The third phase under the same run row (NEU-1387, D-36). Sequencing again, not content:
    the push sender runs with the run id and a pusher, and its clause lands on the detail
    line."""
    _stub_notify(monkeypatch, NotifyResult(alerts_queued=2, push_alerts_queued=2))
    _stub_alert_send(monkeypatch)
    captured = _stub_push_send(
        monkeypatch, PushSendResult(users_considered=1, notifications_sent=2, pushes_delivered=3)
    )
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    assert captured["run_id"] == run_id
    assert captured["pusher"] is not None
    row = await _run_row(session, run_id)
    assert row.status == "succeeded"
    assert row.detail is not None
    assert row.detail.endswith(
        "push: 2 notifications to 1 users, 3 delivered, 0 failed, 0 suppressed, 0 pruned, 0 lost"
    )


async def test_notify_stage_pushes_nothing_when_the_decision_pass_aborted(session, monkeypatch):
    _stub_notify(monkeypatch, NotifyResult(aborted=True, abort_error="aborted after 10"))

    async def must_not_run(**kwargs):
        raise AssertionError("an aborted decision pass must not reach the push sender")

    monkeypatch.setattr("upmovies.pipeline_run.send_queued_pushes", must_not_run)
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    assert (await _run_row(session, run_id)).status == "failed"


async def test_a_broken_mail_provider_still_lets_the_pushes_go_out(session, monkeypatch):
    """The two senders read disjoint halves of the queue and fail for unrelated reasons, so a
    night where Resend is refusing mail should still put the alerts on people's phones. The run
    is `failed` either way, because an aborted send is an operator's problem."""
    _stub_notify(monkeypatch)
    _stub_alert_send(
        monkeypatch, AlertSendResult(aborted=True, abort_error="alert send aborted after 10")
    )
    captured = _stub_push_send(monkeypatch, PushSendResult(notifications_sent=1))
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    assert captured["run_id"] == run_id
    assert (await _run_row(session, run_id)).status == "failed"


async def test_notify_stage_fails_the_run_when_the_push_send_aborted(session, monkeypatch):
    """A push service that is refusing everything fails the run on the same terms the mail
    provider does, so the watermark does not advance past a window nobody was notified about."""
    _stub_notify(monkeypatch)
    _stub_alert_send(monkeypatch)
    _stub_push_send(
        monkeypatch, PushSendResult(aborted=True, abort_error="push send aborted after 10")
    )
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error == "push send aborted after 10"


# --- the VAPID configuration check (D-36) ---


async def _register_browser(session, make_user, email: str = "pushy@example.com") -> None:
    user = await make_user(email=email)
    session.add(
        PushSubscription(user_id=user.id, endpoint="https://push.test/ep", p256dh="k", auth="a")
    )
    await session.commit()


async def test_an_unusable_vapid_config_skips_the_push_send_and_fails_the_run(
    session, monkeypatch, make_user
):
    """A subscription exists and `VAPID_*` does not. The phase is skipped and the run fails, so
    the deadman goes red within the day rather than the slot running green while every
    subscriber hears nothing."""
    await _register_browser(session, make_user)
    _stub_notify(monkeypatch)
    _stub_alert_send(monkeypatch)

    async def must_not_run(**kwargs):
        raise AssertionError("the push send must not run on an unusable VAPID config")

    monkeypatch.setattr("upmovies.pipeline_run.send_queued_pushes", must_not_run)
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(
        run_id, get_settings().model_copy(update={"vapid_private_key": ""})
    )

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "VAPID_PRIVATE_KEY" in row.error
    assert row.detail and "push: not sent" in row.detail


async def test_an_unusable_vapid_config_still_sends_the_mail(session, monkeypatch, make_user):
    """The independence rule in the other direction (NEU-1387): a VAPID typo must not cost the
    night's mail, or the decisions, which are committed before the push phase is even
    considered."""
    await _register_browser(session, make_user)
    _stub_notify(monkeypatch, NotifyResult(alerts_queued=2, push_alerts_queued=2))
    captured = _stub_alert_send(monkeypatch, AlertSendResult(mails_sent=1, sent=2))
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(
        run_id, get_settings().model_copy(update={"vapid_subject": "not-a-url"})
    )

    assert captured["run_id"] == run_id
    row = await _run_row(session, run_id)
    assert row.detail and "2 sent" in row.detail


async def test_the_push_send_runs_without_vapid_when_nobody_has_subscribed(session, monkeypatch):
    """Before the first browser registers the keys are genuinely optional, and failing the slot
    that also sends the night's mail over a capability nobody uses yet would be the shared
    failure mode the slots are kept apart to avoid."""
    _stub_notify(monkeypatch)
    _stub_alert_send(monkeypatch)
    captured = _stub_push_send(monkeypatch)
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(
        run_id, get_settings().model_copy(update={"vapid_private_key": ""})
    )

    assert captured["run_id"] == run_id
    assert (await _run_row(session, run_id)).status == "succeeded"


async def test_notify_stage_marks_run_failed_on_crash(session, monkeypatch):
    """A crashed pass must leave a `failed` run, because that run's `started_at` is the next
    one's watermark — a crash recorded as success would skip the window it never decided."""

    async def boom(**kwargs):
        raise RuntimeError("simulated notify crash")

    monkeypatch.setattr("upmovies.pipeline_run.run_notify_pass", boom)
    run_id = await create_run(session, kind="notify")
    await session.commit()

    await pipeline_run.run_notify_stage(run_id, get_settings())  # must not raise

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated notify crash" in row.error


async def test_run_notify_opens_its_own_run_kind_and_pings_its_own_deadman(session, monkeypatch):
    """Its own kind because the kind *is* the watermark, and its own check because this pass
    failing is silence for the user rather than for the catalogue."""
    kinds: list[str] = []
    pings: list[tuple[str | None, str]] = []

    async def fake_stage(run_id, settings, *args, **kwargs):
        async with pipeline_run.SessionLocal() as s:
            kinds.append(
                (await s.execute(select(IngestRun.kind).where(IngestRun.id == run_id))).scalar_one()
            )
            await finalize_run(s, run_id, status="succeeded")
            await s.commit()

    async def fake_ping(base_url, suffix=""):
        pings.append((base_url, suffix))

    monkeypatch.setattr(pipeline_run, "run_notify_stage", fake_stage)
    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)
    settings = get_settings().model_copy(update={"healthcheck_notify_url": "https://hc/notify"})

    ok = await pipeline_run.run_notify(settings)

    assert ok is True
    assert kinds == ["notify"]
    assert pings == [("https://hc/notify", "/start"), ("https://hc/notify", "")]


def test_main_runs_the_notify_arm(monkeypatch):
    """`python -m upmovies.pipeline_run notify` — the fifth Coolify slot (D-31)."""
    settings = get_settings().model_copy(update=DEFAULT_ROUTING)
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the notify arm must not run the daily chain")

    monkeypatch.setattr("upmovies.pipeline_run.run_daily", must_not_run)
    monkeypatch.setattr("upmovies.pipeline_run.run_notify", _stub_daily(ok=True))
    assert pipeline_run.main(["notify"]) == 0
    monkeypatch.setattr("upmovies.pipeline_run.run_notify", _stub_daily(ok=False))
    assert pipeline_run.main(["notify"]) == 1


def test_main_refuses_the_notify_arm_without_mail_configuration(monkeypatch):
    """Deliberately *not* exempt, unlike the sweep and the poll. The queue this pass fills is
    mail, and discovering a missing `RESEND_API_KEY` in the sender is discovering it one slot
    too late."""
    settings = get_settings().model_copy(
        update={**DEFAULT_ROUTING, "mail_provider": "resend", "resend_api_key": None}
    )
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the notify arm must not start on an unusable mail configuration")

    monkeypatch.setattr("upmovies.pipeline_run.run_notify", must_not_run)

    with pytest.raises(MailConfigurationError):
        pipeline_run.main(["notify"])


# --- digest: M7's digest sender, one slot per cadence (NEU-1381, D-33) ---


def _stub_digest_send(monkeypatch, result: DigestSendResult | None = None) -> dict:
    """Replace the pass itself. What `run_digest_stage` owes is the run row; what the mail
    carries is `tests/integration/app/test_digest_sender.py`'s subject."""
    captured: dict = {}

    async def fake_send(**kwargs):
        captured.update(kwargs)
        return result if result is not None else DigestSendResult(cadence=kwargs["cadence"])

    monkeypatch.setattr("upmovies.pipeline_run.send_digests", fake_send)
    return captured


async def test_digest_stage_finalizes_the_run_with_its_detail_line(session, monkeypatch):
    captured = _stub_digest_send(
        monkeypatch,
        DigestSendResult(cadence="weekly", users_considered=3, mails_sent=2, sent=5, slate_dates=4),
    )
    run_id = await create_run(session, kind="digest")
    await session.commit()

    await pipeline_run.run_digest_stage(run_id, get_settings(), "weekly")

    assert captured["run_id"] == run_id
    assert captured["cadence"] == "weekly"
    assert captured["mailer"] is not None
    row = await _run_row(session, run_id)
    assert row.status == "succeeded"
    assert row.error is None
    assert row.detail == (
        "digest weekly: 2 mails to 3 users, 5 sent, 0 failed, 0 suppressed, 0 gated, "
        "4 slate dates, 0 lost"
    )


async def test_digest_stage_fails_the_run_when_the_pass_aborted(session, monkeypatch):
    _stub_digest_send(
        monkeypatch,
        DigestSendResult(cadence="daily", aborted=True, abort_error="digest send aborted after 10"),
    )
    run_id = await create_run(session, kind="digest")
    await session.commit()

    await pipeline_run.run_digest_stage(run_id, get_settings(), "daily")

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error == "digest send aborted after 10"


async def test_digest_stage_marks_run_failed_on_crash(session, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("simulated digest crash")

    monkeypatch.setattr("upmovies.pipeline_run.send_digests", boom)
    run_id = await create_run(session, kind="digest")
    await session.commit()

    await pipeline_run.run_digest_stage(run_id, get_settings(), "weekly")  # must not raise

    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.error and "simulated digest crash" in row.error


@pytest.mark.parametrize(
    ("cadence", "url_field"),
    [("daily", "healthcheck_digest_daily_url"), ("weekly", "healthcheck_digest_weekly_url")],
)
async def test_run_digest_opens_its_own_run_kind_and_pings_the_cadence_s_deadman(
    session, monkeypatch, cadence, url_field
):
    """One run kind for both cadences, and one deadman *per* cadence — a healthchecks.io check
    has one schedule, so the daily and the weekly slot cannot share it."""
    kinds: list[str] = []
    cadences: list[str] = []
    pings: list[tuple[str | None, str]] = []

    async def fake_stage(run_id, settings, cadence):
        cadences.append(cadence)
        async with pipeline_run.SessionLocal() as s:
            kinds.append(
                (await s.execute(select(IngestRun.kind).where(IngestRun.id == run_id))).scalar_one()
            )
            await finalize_run(s, run_id, status="succeeded")
            await s.commit()

    async def fake_ping(base_url, suffix=""):
        pings.append((base_url, suffix))

    monkeypatch.setattr(pipeline_run, "run_digest_stage", fake_stage)
    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)
    settings = get_settings().model_copy(
        update={
            "healthcheck_digest_daily_url": "https://hc/digest-daily",
            "healthcheck_digest_weekly_url": "https://hc/digest-weekly",
        }
    )

    ok = await pipeline_run.run_digest(settings, cadence)

    assert ok is True
    assert kinds == ["digest"]
    assert cadences == [cadence]
    expected = f"https://hc/digest-{cadence}"
    assert pings == [(expected, "/start"), (expected, "")]


async def test_run_digest_pings_fail_on_failure(session, monkeypatch):
    pings: list[tuple[str | None, str]] = []

    async def fake_stage(run_id, settings, cadence):
        async with pipeline_run.SessionLocal() as s:
            await finalize_run(s, run_id, status="failed", error="boom")
            await s.commit()

    async def fake_ping(base_url, suffix=""):
        pings.append((base_url, suffix))

    monkeypatch.setattr(pipeline_run, "run_digest_stage", fake_stage)
    monkeypatch.setattr(pipeline_run, "_ping", fake_ping)
    settings = get_settings().model_copy(
        update={"healthcheck_digest_weekly_url": "https://hc/digest-weekly"}
    )

    ok = await pipeline_run.run_digest(settings, "weekly")

    assert ok is False
    assert pings == [("https://hc/digest-weekly", "/start"), ("https://hc/digest-weekly", "/fail")]


def test_main_runs_the_digest_arm_with_its_cadence(monkeypatch):
    """`python -m upmovies.pipeline_run digest {daily|weekly}` — the sixth and seventh slots."""
    settings = get_settings().model_copy(update=DEFAULT_ROUTING)
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)
    ran: list[str] = []

    def stub(*, ok: bool):
        async def _run(settings, cadence):
            ran.append(cadence)
            return ok

        return _run

    def must_not_run(*args, **kwargs):
        raise AssertionError("the digest arm must not run the daily chain")

    monkeypatch.setattr("upmovies.pipeline_run.run_daily", must_not_run)
    monkeypatch.setattr("upmovies.pipeline_run.run_digest", stub(ok=True))
    assert pipeline_run.main(["digest", "daily"]) == 0
    assert pipeline_run.main(["digest", "weekly"]) == 0
    monkeypatch.setattr("upmovies.pipeline_run.run_digest", stub(ok=False))
    assert pipeline_run.main(["digest", "weekly"]) == 1
    assert ran == ["daily", "weekly", "weekly"]


@pytest.mark.parametrize("argv", [["digest"], ["digest", "off"], ["digest", "hourly"]])
def test_main_refuses_a_digest_without_a_real_cadence(monkeypatch, capsys, argv):
    """A usage error, answered before any configuration is validated or any run opened."""

    def must_not_run(*args, **kwargs):
        raise AssertionError("a digest with no cadence must not start")

    monkeypatch.setattr("upmovies.pipeline_run.run_digest", must_not_run)
    monkeypatch.setattr("upmovies.pipeline_run.validate_stage_configuration", must_not_run)

    assert pipeline_run.main(argv) == 2
    err = capsys.readouterr().err
    assert "daily" in err and "weekly" in err


def test_main_refuses_the_digest_arm_without_mail_configuration(monkeypatch):
    """Not exempt, for the notify slot's reason: this slot exists to send mail."""
    settings = get_settings().model_copy(
        update={**DEFAULT_ROUTING, "mail_provider": "resend", "resend_api_key": None}
    )
    monkeypatch.setattr("upmovies.pipeline_run.get_settings", lambda: settings)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the digest arm must not start on an unusable mail configuration")

    monkeypatch.setattr("upmovies.pipeline_run.run_digest", must_not_run)

    with pytest.raises(MailConfigurationError):
        pipeline_run.main(["digest", "weekly"])
