"""`/me/follows`: the follow graph's CRUD (D-10), subscriber-only (D-39).

Every route carries `require_entitled()`, applied once at the router so a route added later
cannot forget it; the handlers that need the user take the same dependency object, which FastAPI
resolves once per request. Cookie session plus CSRF on the writes, like `/me` next door."""

from datetime import datetime

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
    follow: Follow,
    label: EntityLabel | None,
    headline: HeadlineRelease | None,
    last_activity: datetime | None,
) -> FollowOut:
    """`label` is `None` for a follow the catalog cannot resolve; the row is still returned, with
    nulls, because nothing here deletes user graph rows (D-40).

    `headline` and `last_activity` have no defaults: every route that builds a `FollowOut` has
    to say what it did about each, so a row cannot quietly come back null from one route and
    filled from another (EF-14, EF-15). The list route batches both; the single-row routes ask
    `follow_service.headline_for` and `follow_service.last_activity_for`."""
    return FollowOut(
        entity_type=follow.entity_type,
        entity_id=follow.entity_id,
        name=None if label is None else label.name,
        image_path=None if label is None else label.image_path,
        headline_release=headline_release_out(headline),
        source=follow.source,
        created_at=follow.created_at,
        last_activity_at=last_activity,
    )


@router.get("", response_model=FollowListResponse)
async def list_follows(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> FollowListResponse:
    """Every follow the user has, in one unpaginated response (EF-15). **Unpaginated by
    decision, not oversight — NEU-1451.** Read this before adding a pass or paging it.

    The follows page is one flat list with type chips, three sorts and a client-side name
    filter, and every follow button in the app (`useIsFollowing`, `useFollow`) answers "do I
    follow X?" by scanning this whole cached list. Paging the route therefore costs a keys view
    and a rewrite of the buttons' optimistic update, cross-repo — not just a `limit`.

    The cost is four linear passes over the list — the follow rows, the entity labels,
    `catalog.headline_release` for the title rows, and `last_activity_at` over `news.event`
    — plus the DTO build and JSON. Measured 2026-09-23 after NEU-1440, all title follows, two
    published cards per film, warm, median of 3:

        follows   service   DTO+JSON   payload   gzipped
          1,000    113 ms       9 ms   0.32 MB         —
          5,000    348 ms      62 ms   1.58 MB   0.19 MB
         10,000    694 ms     110 ms   3.17 MB         —

    `last_activity_at` is the largest pass and the one that grows with the event table rather
    than the follow count (326 ms of 803 at 10,000 follows with ten cards per film).

    10,000 is not reachable: EF-21 lets an import follow only in-window films (`confirm` writes
    `selectable_film_ids` alone), the whole catalog holds about 9,500 of those, and nothing
    bulk-writes entity follows. A heavy importer lands in the hundreds to low thousands. So:
    `GZipMiddleware` (`main.py`) carries the payload, and past
    `follow_service.FOLLOWS_WARN_THRESHOLD` (2,000) the service logs a WARNING per request.

    **Reopen the paging design** — server-side `limit`/cursor with `types`, `q` and `sort`,
    plus a keys view for the buttons, via `upmovies/pagination.py` — if a writer can create
    follows outside the alert window, that warning fires in production, or a fifth pass is
    proposed. Re-take the numbers with `scripts/bench_follows.py`; the full reasoning is
    `docs/specs/NEU-1451-follows-list-ceiling.md`."""
    rows = await follow_service.list_follows(db, user=user)
    return FollowListResponse(
        items=[_to_out(r.follow, r.label, r.headline, r.last_activity) for r in rows],
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
    return _to_out(
        follow,
        label,
        await follow_service.headline_for(db, follow),
        await follow_service.last_activity_for(db, follow),
    )


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
    return _to_out(
        follow,
        label,
        await follow_service.headline_for(db, follow),
        await follow_service.last_activity_for(db, follow),
    )


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
