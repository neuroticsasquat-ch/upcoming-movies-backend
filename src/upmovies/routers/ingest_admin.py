"""ADMIN_TOKEN-protected endpoints that kick off the ingestion pipelines as background
tasks and report run status (for cron polling). The trigger returns immediately with a
run_id; the pipeline runs in an asyncio task that owns its own DB session and always
finalizes the run, even on an unexpected crash."""

import asyncio
import logging
from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.config import Settings, get_settings
from upmovies.db import SessionLocal
from upmovies.deps import get_session, require_admin
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run, finalize_run
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.service import run_tmdb_ingest
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm.client import AnthropicClient
from upmovies.news.fetcher import run_feeds_ingest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/ingest", tags=["admin"], dependencies=[Depends(require_admin)])


def _session_factory() -> AsyncSession:
    return SessionLocal()


async def _finalize_failed(run_id: UUID, error: str) -> None:
    async with SessionLocal() as s:
        await finalize_run(s, run_id, status="failed", error=error)
        await s.commit()


async def _background_tmdb(run_id: UUID, settings: Settings) -> None:
    try:
        today = date.today()
        async with TMDBClient(
            base_url=settings.tmdb_base_url,
            api_key=settings.tmdb_api_key,
            rate_calls=settings.tmdb_rate_limit_requests,
            rate_window=settings.tmdb_rate_limit_window_seconds,
            retry_max_attempts=settings.tmdb_retry_max_attempts,
        ) as client:
            await run_tmdb_ingest(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                release_date_gte=today - timedelta(days=settings.tmdb_release_window_past_days),
                release_date_lte=today + timedelta(days=settings.tmdb_release_window_future_days),
                min_popularity=settings.tmdb_min_popularity,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
                excluded_statuses=settings.tmdb_excluded_statuses,
            )
    except Exception as e:
        log.exception("background tmdb ingest crashed")
        await _finalize_failed(run_id, str(e))


async def _background_feeds(
    run_id: UUID, settings: Settings, per_film_override: bool | None = None
) -> None:
    try:
        await run_feeds_ingest(
            session_factory=_session_factory,
            run_id=run_id,
            recency_days=settings.feed_recency_days,
            per_film_enabled=per_film_override
            if per_film_override is not None
            else settings.feeds_per_film_enabled,
            per_film_throttle=settings.feeds_per_film_throttle_seconds,
        )
    except Exception as e:
        log.exception("background feeds ingest crashed")
        await _finalize_failed(run_id, str(e))


async def _background_link(run_id: UUID, settings: Settings) -> None:
    try:
        async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
            await run_link_ingest(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                model=settings.link_model,
                cluster_model=settings.cluster_model,
                recency_days=settings.link_recency_days,
                batch_size=settings.link_batch_size,
                floor=settings.link_confidence_floor,
            )
    except Exception as e:
        log.exception("background link ingest crashed")
        await _finalize_failed(run_id, str(e))


@router.post("/tmdb", status_code=status.HTTP_202_ACCEPTED)
async def trigger_tmdb(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="tmdb")
    await session.commit()
    asyncio.create_task(_background_tmdb(run_id, settings))
    return {"run_id": str(run_id)}


@router.post("/feeds", status_code=status.HTTP_202_ACCEPTED)
async def trigger_feeds(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    per_film: bool | None = Query(default=None),
) -> dict[str, str]:
    run_id = await create_run(session, kind="feeds")
    await session.commit()
    asyncio.create_task(_background_feeds(run_id, settings, per_film))
    return {"run_id": str(run_id)}


@router.post("/link", status_code=status.HTTP_202_ACCEPTED)
async def trigger_link(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="link")
    await session.commit()
    asyncio.create_task(_background_link(run_id, settings))
    return {"run_id": str(run_id)}


@router.get("/{run_id}")
async def get_run_status(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    row = (
        await session.execute(select(IngestRun).where(IngestRun.id == run_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "items_processed": row.items_processed,
        "items_failed": row.items_failed,
        "detail": row.detail,
        "error": row.error,
    }
