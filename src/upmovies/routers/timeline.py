"""`GET /me/timeline`: the grouped feed filtered by the user's follows (D-11, D-12),
subscriber-only (D-39).

The gate is applied once at the router and the handler takes the same dependency object, as in
`routers/follows.py`. Read-only, so no CSRF; the response is `/feed/grouped`'s exactly, which is
what lets the client swap one for the other on `/` once `me` resolves."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import require_entitled
from upmovies.app.models import User
from upmovies.deps import get_session
from upmovies.public import service
from upmovies.public.dto import FeedDayResponse

entitled = require_entitled()

router = APIRouter(prefix="/me/timeline", tags=["me"], dependencies=[Depends(entitled)])


@router.get("", response_model=FeedDayResponse)
async def get_timeline(
    # limit/offset count distinct days, as on `/feed/grouped` — same bounds, so a client
    # switching between the two keeps its page size.
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(entitled),
    session: AsyncSession = Depends(get_session),
) -> FeedDayResponse:
    return await service.get_timeline(session, user_id=user.id, limit=limit, offset=offset)
