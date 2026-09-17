"""`/me/watchlist`: the watchlist's CRUD (D-13, D-14), subscriber-only (D-39).

The gate is applied once at the router and the handlers take the same dependency object, as in
`routers/follows.py`. Cookie session plus CSRF on the writes."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import (
    WatchlistCreateRequest,
    WatchlistFilmOut,
    WatchlistItemOut,
    WatchlistListResponse,
    WatchlistUpdateRequest,
)
from upmovies.app.entitlements import require_entitled
from upmovies.app.errors import NotFound
from upmovies.app.models import User, WatchlistItem
from upmovies.app.services import watchlist_service
from upmovies.catalog.models import Film
from upmovies.deps import get_session, require_csrf

entitled = require_entitled()

router = APIRouter(prefix="/me/watchlist", tags=["me"], dependencies=[Depends(entitled)])


def _to_out(item: WatchlistItem, film: Film) -> WatchlistItemOut:
    return WatchlistItemOut(
        film=WatchlistFilmOut(
            id=film.id,
            tmdb_id=film.tmdb_id,
            slug=film.slug,
            title=film.title,
            poster_path=film.poster_path,
            release_date=film.release_date,
        ),
        source=item.source,
        alert_prefs=item.alert_prefs,
        created_at=item.created_at,
    )


@router.get("", response_model=WatchlistListResponse)
async def list_watchlist(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> WatchlistListResponse:
    items = await watchlist_service.list_items(db, user=user)
    return WatchlistListResponse(items=[_to_out(item, film) for item, film in items])


@router.post(
    "",
    response_model=WatchlistItemOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
async def add_to_watchlist(
    payload: WatchlistCreateRequest,
    response: Response,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> WatchlistItemOut:
    """Add a film. 201 with the new row, or 200 with the existing one, untouched — a toggle
    clicked twice should not fail, and PATCH is how prefs change."""
    try:
        item, film, created = await watchlist_service.add(
            db, user=user, film_id=payload.film_id, alert_prefs=payload.alert_prefs
        )
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="film_not_found"
        ) from None
    if not created:
        response.status_code = status.HTTP_200_OK
    return _to_out(item, film)


@router.patch(
    "/{film_id}",
    response_model=WatchlistItemOut,
    dependencies=[Depends(require_csrf)],
)
async def update_alert_prefs(
    film_id: UUID,
    payload: WatchlistUpdateRequest,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> WatchlistItemOut:
    try:
        item, film = await watchlist_service.set_alert_prefs(
            db, user=user, film_id=film_id, alert_prefs=payload.alert_prefs
        )
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="watchlist_item_not_found"
        ) from None
    return _to_out(item, film)


@router.delete(
    "/{film_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def remove_from_watchlist(
    film_id: UUID,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Remove a film. If the follow graph added it, this also records a dismissal so it is not
    derived again (D-13)."""
    try:
        await watchlist_service.remove(db, user=user, film_id=film_id)
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="watchlist_item_not_found"
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
