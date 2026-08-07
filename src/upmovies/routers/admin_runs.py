"""Session + is_admin protected, read-only ingest-run endpoints for the admin UI.

Deliberately separate from the ADMIN_TOKEN trigger/poll endpoints (ingest_admin):
those are machine-facing (cron); these are human-facing and gated by a session +
the `is_admin` flag via `require_current_admin`."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from upmovies.deps import get_session, require_current_admin
from upmovies.ingest.dto import RetrievalHealthOut, RetrievalHealthPointOut, RunOut
from upmovies.ingest.models import IngestRun
from upmovies.ingest.retrieval_health import load_retrieval_health, retrieval_health_series

router = APIRouter(
    prefix="/admin/runs",
    tags=["admin"],
    dependencies=[Depends(require_current_admin)],
)


def _to_out(run: IngestRun, health: RetrievalHealthOut | None) -> RunOut:
    """Assemble the read model. Retrieval health is attached rather than validated off the
    run, because half of it is an aggregate over `ingest.link_retrieval_probe` (NEU-997)."""
    out = RunOut.model_validate(run)
    out.retrieval_health = health
    return out


# Declared ahead of `/{run_id}`: routes match in declaration order, so a literal path that
# is not a UUID would otherwise be swallowed by the detail route and 422.
@router.get("/retrieval-health", response_model=list[RetrievalHealthPointOut])
async def list_retrieval_health(
    limit: int = Query(default=30, ge=1, le=200),
    since: datetime | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> list[RetrievalHealthPointOut]:
    """The retrieval-health trend, newest first — one point per run that ran retrieval.

    A separate series rather than a filter over the run list because the failure this guards
    is slow drift (ADR-0010): as the catalog grows and titles collide, the zero-candidate
    rate creeps up and stories are rejected with no model ever seeing them. A single run's
    number tells you nothing; the shape over a fortnight tells you everything.

    `since` names that fortnight directly; `limit` bounds the page whether or not it is
    given. A naive `since` is read as UTC, so a caller that omits the offset gets the
    window it meant rather than one shifted by the server's timezone."""
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return await retrieval_health_series(db, limit=limit, since=since)


@router.get("", response_model=list[RunOut])
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_session),
) -> list[RunOut]:
    rows = (
        await db.execute(
            select(IngestRun)
            .options(selectinload(IngestRun.llm_usage))
            .order_by(IngestRun.started_at.desc())
            .limit(limit)
        )
    ).scalars()
    runs = list(rows)
    health = await load_retrieval_health(db, [run.id for run in runs])
    return [_to_out(run, health.get(run.id)) for run in runs]


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_session),
) -> RunOut:
    row = (
        await db.execute(
            select(IngestRun)
            .options(selectinload(IngestRun.llm_usage))
            .where(IngestRun.id == run_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    health = await load_retrieval_health(db, [row.id])
    return _to_out(row, health.get(row.id))
