"""Session + is_admin + CSRF protected endpoints for correcting bad story↔film links from the
admin UI. Human-facing (cookie + is_admin via `require_current_admin`), distinct from the
ADMIN_TOKEN machine endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import User
from upmovies.deps import get_session, require_csrf, require_current_admin
from upmovies.link import moderation

SUMMARY_MAX_LEN = 500

router = APIRouter(
    prefix="/admin/events",
    tags=["admin"],
    dependencies=[Depends(require_current_admin), Depends(require_csrf)],
)


class DelinkBody(BaseModel):
    url: str


class DelinkResponse(BaseModel):
    delinked: int
    event_removed: bool
    resummarize_queued: bool


class EditSummaryBody(BaseModel):
    summary: str

    @field_validator("summary")
    @classmethod
    def _non_empty_bounded(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("summary must not be empty")
        if len(stripped) > SUMMARY_MAX_LEN:
            raise ValueError(f"summary must be at most {SUMMARY_MAX_LEN} characters")
        return stripped


class SummaryOut(BaseModel):
    summary: str
    edited: bool
    edited_at: datetime | None


@router.post("/{event_id}/delink", response_model=DelinkResponse)
async def delink_source(
    event_id: UUID, body: DelinkBody, db: AsyncSession = Depends(get_session)
) -> DelinkResponse:
    try:
        result = await moderation.delink_story(db, event_id=event_id, url=body.url)
    except moderation.EventNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event_not_found"
        ) from None
    except moderation.StoryNotInEvent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="story_not_in_event"
        ) from None
    await db.commit()
    return DelinkResponse(**vars(result))


@router.delete("/{event_id}", response_model=DelinkResponse)
async def delete_event(event_id: UUID, db: AsyncSession = Depends(get_session)) -> DelinkResponse:
    try:
        result = await moderation.delete_event(db, event_id=event_id)
    except moderation.EventNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event_not_found"
        ) from None
    await db.commit()
    return DelinkResponse(**vars(result))


@router.patch("/{event_id}/summary", response_model=SummaryOut)
async def edit_summary(
    event_id: UUID,
    body: EditSummaryBody,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_current_admin),
) -> SummaryOut:
    try:
        row = await moderation.edit_summary(
            db, event_id=event_id, summary=body.summary, user_id=user.id
        )
    except moderation.EventNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event_not_found"
        ) from None
    await db.commit()
    return SummaryOut(summary=row.summary, edited=True, edited_at=row.edited_at)


@router.delete("/{event_id}/summary", status_code=status.HTTP_204_NO_CONTENT)
async def reset_summary(event_id: UUID, db: AsyncSession = Depends(get_session)) -> None:
    try:
        await moderation.reset_summary(db, event_id=event_id)
    except moderation.EventNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event_not_found"
        ) from None
    except moderation.SummaryNotEdited:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="summary_not_edited"
        ) from None
    await db.commit()
