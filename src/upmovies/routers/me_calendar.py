"""`GET /me/calendar`: the **my films calendar** (D-34, D-39) — the release calendar narrowed
to the films the caller follows by title.

`CalendarResponse` exactly as `/calendar` answers it, with the same `limit`/`offset` bounds and
the same date-paging, so the tabbed calendar page can render "My films" and "All releases"
through one component and one set of grouping helpers. What differs is the film set, and only
that: the caller's title follows (EF-14), drawn the way the subscribed `.ics` feed draws them.

Shaped like `routers/timeline.py` rather than living beside `/calendar` in `routers/public.py`:
the gate is applied once at the router, so a second `/me/calendar/*` route added later cannot
forget it, and the handler takes the same dependency object to get the user. Read-only, so no
CSRF. No `rate_limit` — no `/me/*` route carries one; the public bucket meters the anonymous
traffic this route has none of.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import require_entitled
from upmovies.app.models import User
from upmovies.deps import get_session
from upmovies.public import service
from upmovies.public.dto import CalendarResponse

entitled = require_entitled()

router = APIRouter(prefix="/me/calendar", tags=["me"], dependencies=[Depends(entitled)])


@router.get("", response_model=CalendarResponse)
async def get_my_calendar(
    # limit/offset count distinct release dates (soonest first), not film rows — the public
    # route's meaning and its bounds, so a client switching tabs keeps its page size.
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(entitled),
    session: AsyncSession = Depends(get_session),
) -> CalendarResponse:
    """Following no films, or none with anything upcoming, is a 200 with no items — never an
    error. The refusals are the gate's: 401 with no session, 403 `entitlement_required`
    without a live grant (D-39)."""
    return await service.get_my_films_calendar(session, user_id=user.id, limit=limit, offset=offset)
