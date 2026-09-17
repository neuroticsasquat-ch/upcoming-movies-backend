"""`/me/follows`: the follow graph's CRUD (D-10), subscriber-only (D-39).

Every route carries `require_entitled()`, applied once at the router so a route added later
cannot forget it; the handlers that need the user take the same dependency object, which FastAPI
resolves once per request. Cookie session plus CSRF on the writes, like `/me` next door."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import (
    FollowCreateRequest,
    FollowEntityType,
    FollowListResponse,
    FollowOut,
    normalise_entity_id,
)
from upmovies.app.entitlements import require_entitled
from upmovies.app.errors import NotFound
from upmovies.app.models import Follow, User
from upmovies.app.services import follow_service
from upmovies.deps import get_session, require_csrf

entitled = require_entitled()

router = APIRouter(prefix="/me/follows", tags=["me"], dependencies=[Depends(entitled)])


def _to_out(follow: Follow) -> FollowOut:
    return FollowOut(
        entity_type=follow.entity_type,
        entity_id=follow.entity_id,
        source=follow.source,
        created_at=follow.created_at,
    )


@router.get("", response_model=FollowListResponse)
async def list_follows(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> FollowListResponse:
    items = await follow_service.list_follows(db, user=user)
    return FollowListResponse(items=[_to_out(f) for f in items])


@router.post(
    "",
    response_model=FollowOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
async def create_follow(
    payload: FollowCreateRequest,
    response: Response,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> FollowOut:
    """Follow an entity. 201 with the new row, or 200 with the existing one: a follow button
    that is clicked twice should not fail the second time, and the caller can tell which."""
    try:
        follow, created = await follow_service.follow(
            db, user=user, entity_type=payload.entity_type, entity_id=payload.entity_id
        )
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="entity_not_found"
        ) from None
    if not created:
        response.status_code = status.HTTP_200_OK
    return _to_out(follow)


@router.delete(
    "/{entity_type}/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def delete_follow(
    entity_type: FollowEntityType,
    entity_id: str,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        canonical = normalise_entity_id(entity_type, entity_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_entity_id"
        ) from None
    try:
        await follow_service.unfollow(db, user=user, entity_type=entity_type, entity_id=canonical)
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="follow_not_found"
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
