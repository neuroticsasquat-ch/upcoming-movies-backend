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
    headline_release_out,
    normalise_entity_id,
)
from upmovies.app.entitlements import require_entitled
from upmovies.app.errors import NotFound
from upmovies.app.models import Follow, User
from upmovies.app.repos.follow_repo import EntityLabel
from upmovies.app.services import follow_service
from upmovies.catalog.headline_release import HeadlineRelease
from upmovies.deps import get_session, require_csrf

entitled = require_entitled()

router = APIRouter(prefix="/me/follows", tags=["me"], dependencies=[Depends(entitled)])


def _to_out(
    follow: Follow, label: EntityLabel | None, headline: HeadlineRelease | None
) -> FollowOut:
    """`label` is `None` for a follow the catalog cannot resolve; the row is still returned, with
    nulls, because nothing here deletes user graph rows (D-40).

    `headline` has no default: every route that builds a `FollowOut` has to say what it did
    about the date, so a title row cannot quietly come back null from one route and filled from
    another (EF-14). The list route batches them; the single-row routes ask
    `follow_service.headline_for`."""
    return FollowOut(
        entity_type=follow.entity_type,
        entity_id=follow.entity_id,
        name=None if label is None else label.name,
        image_path=None if label is None else label.image_path,
        headline_release=headline_release_out(headline),
        source=follow.source,
        created_at=follow.created_at,
    )


@router.get("", response_model=FollowListResponse)
async def list_follows(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> FollowListResponse:
    rows = await follow_service.list_follows(db, user=user)
    return FollowListResponse(
        items=[_to_out(r.follow, r.label, r.headline) for r in rows],
    )


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

    A `coverage` in the body is ignored rather than refused (EF-1) — `FollowCreateRequest`
    never declares it, so the 422 D-1414.6 raised here is gone with the tier it guarded."""
    try:
        follow, label, created = await follow_service.follow(
            db,
            user=user,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
        )
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="entity_not_found"
        ) from None
    if not created:
        response.status_code = status.HTTP_200_OK
    return _to_out(follow, label, await follow_service.headline_for(db, follow))


@router.patch(
    "/{entity_type}/{entity_id}",
    response_model=FollowOut,
    dependencies=[Depends(require_csrf)],
)
async def update_follow(
    entity_type: FollowEntityType,
    entity_id: str,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> FollowOut:
    """**A no-op that answers 200 with the follow unchanged** (EF-1).

    A binary follow has nothing left to PATCH: `coverage` was its only mutable field. The route
    survives its own purpose for the length of the M2→M3 gap because the live frontend still
    sends this request (`updateFollowCoverage`, NEU-1415), and a 404 or a 405 would surface as
    an error toast on a control the user is about to lose anyway. The M3 frontend ticket
    deletes the caller; this route goes with it.

    Still 404s for a follow that does not exist, and still validates the id: answering 200 for
    a row that is not there would be the one lie worse than doing nothing."""
    try:
        canonical = normalise_entity_id(entity_type, entity_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_entity_id"
        ) from None
    try:
        follow, label = await follow_service.get_follow(
            db, user=user, entity_type=entity_type, entity_id=canonical
        )
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="follow_not_found"
        ) from None
    return _to_out(follow, label, await follow_service.headline_for(db, follow))


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
