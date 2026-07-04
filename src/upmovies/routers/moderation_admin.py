"""Session + is_admin + CSRF protected endpoints for correcting bad story↔film links from the
admin UI. Human-facing (cookie + is_admin via `require_current_admin`), distinct from the
ADMIN_TOKEN machine endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.deps import get_session, require_csrf, require_current_admin
from upmovies.link import moderation

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
