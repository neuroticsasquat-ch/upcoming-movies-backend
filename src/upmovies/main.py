import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.rate_limit import (
    RateLimited,
    rate_limited_handler,
    validate_rate_limit_configuration,
)
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.ingest.runs import mark_stale_runs_cancelled
from upmovies.llm import validate_stage_configuration
from upmovies.logging_config import configure_logging
from upmovies.mail import MailGateway, validate_mail_configuration
from upmovies.routers import (
    admin_runs,
    auth,
    credit_holds_admin,
    follows,
    health,
    imports,
    imports_tmdb,
    ingest_admin,
    invites_admin,
    me,
    moderation_admin,
    public,
    resolution_admin,
    sources_admin,
    timeline,
    user_settings,
    users_admin,
    watchlist,
)

if dsn := os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=dsn,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        traces_sample_rate=0.1,
        environment=os.environ.get("ENVIRONMENT", "development"),
        release=os.environ.get("GIT_SHA", "unknown"),
    )


async def run_startup_cleanup(session: AsyncSession, stale_after_minutes: int) -> int:
    """Cancel runs left `running` by a crash/restart so they don't block forever."""
    return await mark_stale_runs_cancelled(session, stale_after_minutes=stale_after_minutes)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Before anything else, and before the app can serve: a stage routed at a
    # `(provider, model)` with no rates entry or no credential is a container that must not
    # start (NEU-981, spec §7). Left to the run, the same fault surfaces as a `KeyError`
    # partway through a nightly publish, after the stages have already committed part of
    # their work. `pipeline_run.main` runs the same check for its own sake — the scheduled
    # tasks are a separate process, and this lifespan is not on their path.
    validate_stage_configuration(settings)
    # And the same guard for the mail configuration (NEU-1338, D-30). It is the stronger case
    # of the two: an LLM stage discovered unroutable mid-run loses a nightly publish, while a
    # mail credential discovered mid-signup loses it *after* the user row is committed and
    # before the verification mail exists — a state the account flow cannot retry out of.
    validate_mail_configuration(settings)
    # And the buckets (NEU-1344, D-19). Cheapest of the three — it parses six strings and
    # reaches nothing — but the same argument: a malformed limit is either no limit at all or
    # a route that refuses everybody, and neither is a thing to discover from traffic.
    validate_rate_limit_configuration(settings)
    async with SessionLocal() as session:
        await run_startup_cleanup(session, stale_after_minutes=settings.ingest_stale_run_minutes)
        await session.commit()
    # One gateway for the process, not one per request: it owns an httpx connection pool, and
    # the pool is the whole reason not to build one per signup. Hung off `app.state` rather
    # than a module global so `deps.get_mailer` can reach it from the request and a test can
    # put its own there — and so that this `async with` is what closes the pool on shutdown.
    async with MailGateway(settings) as mailer:
        app.state.mailer = mailer
        try:
            yield
        finally:
            # Cleared, not just closed. `app` is a module-level singleton, so a closed gateway
            # left here outlives the lifespan that owned it — and `deps.get_mailer` only
            # checks for None, so the next caller would get the `RuntimeError` from a closed
            # gateway instead of the `mail_unavailable` 500 that says what to do about it.
            app.state.mailer = None


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="upmovies-backend", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
    # The 429 the rate-limit dependency raises. A handler rather than an `HTTPException`
    # because the body carries the bucket and the wait alongside the detail string, and
    # FastAPI's own handler renders only `{"detail": ...}` (`app/rate_limit.py`).
    app.add_exception_handler(RateLimited, rate_limited_handler)
    app.include_router(health.router)
    app.include_router(ingest_admin.router)
    app.include_router(admin_runs.router)
    app.include_router(credit_holds_admin.router)
    app.include_router(invites_admin.router)
    app.include_router(moderation_admin.router)
    app.include_router(resolution_admin.router)
    app.include_router(sources_admin.router)
    app.include_router(users_admin.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(follows.router)
    app.include_router(user_settings.router)
    app.include_router(watchlist.router)
    app.include_router(imports.router)
    app.include_router(imports_tmdb.router)
    app.include_router(timeline.router)
    app.include_router(public.router)
    return app


app = create_app()
