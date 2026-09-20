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
    FollowUpdateRequest,
    normalise_entity_id,
)
from upmovies.app.entitlements import require_entitled
from upmovies.app.errors import NotFound
from upmovies.app.models import Follow, User
from upmovies.app.repos.follow_repo import EntityLabel
from upmovies.app.services import follow_service
from upmovies.deps import get_session, require_csrf

entitled = require_entitled()

router = APIRouter(prefix="/me/follows", tags=["me"], dependencies=[Depends(entitled)])


def _to_out(follow: Follow, label: EntityLabel | None) -> FollowOut:
    """`label` is `None` for a follow the catalog cannot resolve; the row is still returned, with
    nulls, because nothing here deletes user graph rows (D-40)."""
    return FollowOut(
        entity_type=follow.entity_type,
        entity_id=follow.entity_id,
        name=None if label is None else label.name,
        image_path=None if label is None else label.image_path,
        source=follow.source,
        coverage=follow.coverage,
        created_at=follow.created_at,
    )


@router.get("", response_model=FollowListResponse)
async def list_follows(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> FollowListResponse:
    items = await follow_service.list_follows(db, user=user)
    return FollowListResponse(items=[_to_out(f, label) for f, label in items])


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
    that is clicked twice should not fail the second time, and the caller can tell which.

    `422 coverage_not_applicable` for a `coverage` on any type but `person`, exactly as the
    PATCH below refuses it (D-1414.6)."""
    if payload.coverage is not None and payload.entity_type != "person":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="coverage_not_applicable"
        )
    try:
        follow, label, created = await follow_service.follow(
            db,
            user=user,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            coverage=payload.coverage,
        )
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="entity_not_found"
        ) from None
    if not created:
        response.status_code = status.HTTP_200_OK
    return _to_out(follow, label)


@router.patch(
    "/{entity_type}/{entity_id}",
    response_model=FollowOut,
    dependencies=[Depends(require_csrf)],
)
async def update_follow(
    entity_type: FollowEntityType,
    entity_id: str,
    payload: FollowUpdateRequest,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> FollowOut:
    """Set a person follow's coverage (D-43). 200 with the whole row, so the control that
    changed redraws from one response.

    `422 coverage_not_applicable` for the other three entity types — they name one thing each,
    so there is nothing to narrow, and storing a value nothing reads while answering `200`
    would tell the client it had changed something."""
    if entity_type != "person":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="coverage_not_applicable"
        )
    try:
        canonical = normalise_entity_id(entity_type, entity_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_entity_id"
        ) from None
    try:
        follow, label = await follow_service.set_coverage(
            db,
            user=user,
            entity_type=entity_type,
            entity_id=canonical,
            coverage=payload.coverage,
        )
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="follow_not_found"
        ) from None
    return _to_out(follow, label)


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
