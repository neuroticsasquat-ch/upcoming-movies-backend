import logging
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


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )


async def run_startup_cleanup(session: AsyncSession, stale_after_minutes: int) -> int:
    """Cancel runs left `running` by a crash/restart so they don't block forever."""
    return await mark_stale_runs_cancelled(session, stale_after_minutes=stale_after_minutes)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    async with SessionLocal() as session:
        await run_startup_cleanup(session, stale_after_minutes=settings.ingest_stale_run_minutes)
        await session.commit()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)
    app = FastAPI(title="upmovies-backend", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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
