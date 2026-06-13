"""Session + is_admin protected, read-only ingest-run endpoints for the admin UI.

Deliberately separate from the ADMIN_TOKEN trigger/poll endpoints (ingest_admin):
those are machine-facing (cron); these are human-facing and gated by a session +
the `is_admin` flag via `require_current_admin`."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.deps import get_session, require_current_admin
from upmovies.ingest.dto import RunOut
from upmovies.ingest.models import IngestRun

router = APIRouter(
    prefix="/admin/runs",
    tags=["admin"],
    dependencies=[Depends(require_current_admin)],
)


@router.get("", response_model=list[RunOut])
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_session),
) -> list[IngestRun]:
    rows = (
        await db.execute(select(IngestRun).order_by(IngestRun.started_at.desc()).limit(limit))
    ).scalars()
    return list(rows)


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_session),
) -> IngestRun:
    row = (await db.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return row
