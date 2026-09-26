from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.gateway import DEFAULT_ROUTING
from upmovies.config import get_settings
from upmovies.ingest.models import IngestRun
from upmovies.llm import StageConfigurationError
from upmovies.mail import MailConfigurationError, MailGateway
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


# --- mail configuration validation at startup (NEU-1338, D-30) -----------------

# What a deploy that has turned mail on looks like. Applied with `Settings.model_copy` rather
# than read from the container's env, for the same reason `DEFAULT_ROUTING` is: a locally set
# MAIL_PROVIDER must not decide whether a happy-path test passes.
RESEND_MAIL: dict[str, object] = {
    "mail_provider": "resend",
    "mail_from": "Backlotter <no-reply@upmovies.test>",
    "resend_api_key": "re_test",
    "api_base_url": "https://api.upmovies.test",
}
NOOP_MAIL: dict[str, object] = {
    "mail_provider": "noop",
    "mail_from": "",
    "resend_api_key": None,
}


async def test_lifespan_puts_a_mailer_on_app_state_for_the_routes_to_depend_on(monkeypatch):
    """`MailGateway` is injectable because the lifespan is what owns its connection pool —
    one per process, closed on shutdown, reached from a request through `deps.get_mailer`."""
    settings = get_settings().model_copy(update={**DEFAULT_ROUTING, **NOOP_MAIL})
    monkeypatch.setattr("upmovies.main.get_settings", lambda: settings)

    async with lifespan(app):
        assert isinstance(app.state.mailer, MailGateway)
        assert app.state.mailer.provider == "noop"


async def test_lifespan_refuses_to_start_with_a_mail_provider_it_has_no_key_for(monkeypatch):
    """Without this the container starts, a user signs up, their row commits, and the
    verification mail they are now waiting for fails — the one state the account flow cannot
    retry out of."""
    settings = get_settings().model_copy(
        update={**DEFAULT_ROUTING, **RESEND_MAIL, "resend_api_key": None}
    )
    monkeypatch.setattr("upmovies.main.get_settings", lambda: settings)

    with pytest.raises(MailConfigurationError, match="RESEND_API_KEY"):
        async with lifespan(app):
            pass


async def test_lifespan_refuses_to_start_with_an_undeliverable_sender(monkeypatch):
    settings = get_settings().model_copy(
        update={**DEFAULT_ROUTING, **RESEND_MAIL, "mail_from": "Backlotter"}
    )
    monkeypatch.setattr("upmovies.main.get_settings", lambda: settings)

    with pytest.raises(MailConfigurationError, match="MAIL_FROM"):
        async with lifespan(app):
            pass


async def test_lifespan_clears_the_mailer_on_shutdown(monkeypatch):
    """`app` is a module-level singleton, so a closed gateway left on its state outlives the
    lifespan that owned it — and the next caller would get `RuntimeError: ... is closed`
    instead of the `mail_unavailable` 500 that says what to do about it."""
    settings = get_settings().model_copy(update={**DEFAULT_ROUTING, **NOOP_MAIL})
    monkeypatch.setattr("upmovies.main.get_settings", lambda: settings)

    async with lifespan(app):
        pass

    assert app.state.mailer is None
