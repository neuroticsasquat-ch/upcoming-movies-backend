"""ADMIN_TOKEN-protected endpoints that kick off the ingestion pipelines as background
tasks and report run status (for cron polling). The trigger returns immediately with a
run_id; the pipeline runs in an asyncio task that owns its own DB session and always
finalizes the run, even on an unexpected crash.

The stage runners themselves live in `upmovies.pipeline_run` (shared with the in-process
Coolify scheduled tasks); these endpoints just create a run row and spawn one as a task."""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.config import Settings, get_settings
from upmovies.deps import get_session, require_admin
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.pipeline_run import (
    run_feeds_stage,
    run_link_stage,
    run_synthesize_stage,
    run_tmdb_stage,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/ingest", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/tmdb", status_code=status.HTTP_202_ACCEPTED)
async def trigger_tmdb(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="tmdb")
    await session.commit()
    asyncio.create_task(run_tmdb_stage(run_id, settings))
    return {"run_id": str(run_id)}


@router.post("/feeds", status_code=status.HTTP_202_ACCEPTED)
async def trigger_feeds(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    per_film: bool | None = Query(default=None),
) -> dict[str, str]:
    run_id = await create_run(session, kind="feeds")
    await session.commit()
    asyncio.create_task(run_feeds_stage(run_id, settings, per_film))
    return {"run_id": str(run_id)}


@router.post("/link", status_code=status.HTTP_202_ACCEPTED)
async def trigger_link(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="link")
    await session.commit()
    asyncio.create_task(run_link_stage(run_id, settings))
    return {"run_id": str(run_id)}


@router.post("/synthesize", status_code=status.HTTP_202_ACCEPTED)
async def trigger_synthesize(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run_id = await create_run(session, kind="synthesize")
    await session.commit()
    asyncio.create_task(run_synthesize_stage(run_id, settings))
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
