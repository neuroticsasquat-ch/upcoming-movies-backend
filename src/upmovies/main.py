import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.ingest.runs import mark_stale_runs_cancelled
from upmovies.llm import validate_stage_configuration
from upmovies.logging_config import configure_logging
from upmovies.routers import (
    admin_runs,
    auth,
    health,
    ingest_admin,
    invites_admin,
    me,
    moderation_admin,
    public,
    sources_admin,
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
    async with SessionLocal() as session:
        await run_startup_cleanup(session, stale_after_minutes=settings.ingest_stale_run_minutes)
        await session.commit()
    yield


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
    app.include_router(health.router)
    app.include_router(ingest_admin.router)
    app.include_router(admin_runs.router)
    app.include_router(invites_admin.router)
    app.include_router(moderation_admin.router)
    app.include_router(sources_admin.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(public.router)
    return app


app = create_app()
