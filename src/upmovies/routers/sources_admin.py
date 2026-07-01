"""Session + is_admin + CSRF protected endpoints for viewing source-domain trust tiers and
setting per-domain admin overrides (the backend for NEU-456's admin Sources page). Distinct
from the ADMIN_TOKEN machine endpoints."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.deps import get_session, require_csrf, require_current_admin
from upmovies.news import source_quality
from upmovies.news.models import SourceDomain

router = APIRouter(
    prefix="/admin/sources",
    tags=["admin"],
    dependencies=[Depends(require_current_admin), Depends(require_csrf)],
)


class SourceDomainOut(BaseModel):
    domain: str
    llm_tier: str | None
    llm_reason: str | None
    admin_override: str
    updated_at: datetime


class OverrideBody(BaseModel):
    override: Literal["none", "block", "allow", "trust"]


def _to_out(row: SourceDomain) -> SourceDomainOut:
    return SourceDomainOut(
        domain=row.domain,
        llm_tier=row.llm_tier,
        llm_reason=row.llm_reason,
        admin_override=row.admin_override,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[SourceDomainOut])
async def list_sources(db: AsyncSession = Depends(get_session)) -> list[SourceDomainOut]:
    rows = await source_quality.list_source_domains(db)
    return [_to_out(r) for r in rows]


@router.post("/{domain}/override", response_model=SourceDomainOut)
async def set_source_override(
    domain: str, body: OverrideBody, db: AsyncSession = Depends(get_session)
) -> SourceDomainOut:
    row = await source_quality.set_override(
        db, domain=domain, override=body.override, now=datetime.now(UTC)
    )
    await db.commit()
    return _to_out(row)
