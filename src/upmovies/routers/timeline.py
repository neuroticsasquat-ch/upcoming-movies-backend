"""`GET /me/timeline`: the grouped feed over what the user's follows deliver (EF-3, D-12),
subscriber-only (D-39).

A timeline row is a feed row with a reach (FB-13, ADR-0022): one per (reach, film, day,
section), `via` null for a title follow and naming the person, studio or franchise otherwise. A
film-day reached both ways is two rows. The DTO is `/feed/grouped`'s — whose rows all carry
`via: null` — and day pagination means what it means there, which is what lets the client
render either on `/` once `me` resolves.

The gate is applied once at the router and the handler takes the same dependency object, as in
`routers/follows.py`. Read-only, so no CSRF."""

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
