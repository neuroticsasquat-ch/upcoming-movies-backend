"""`/me/watchlist`: the computed watchlist and the two verbs that change it (D-42, D-45),
subscriber-only (D-39).

Three routes, not four. `GET` reads the set `app.follow_queries` computes; `POST` is **want**
and `DELETE` is **stop**, and both answer the film's resulting state so a client reconciles
from one response. The `PATCH` that set an item's alert preferences is gone with the table it
wrote to — the stores are one setting per user now (`/me/settings`, D-44).

The gate is applied once at the router and the handlers take the same dependency object, as in
`routers/follows.py`. Cookie session plus CSRF on the writes."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import (
    HeadlineReleaseOut,
    WatchlistCoverOut,
    WatchlistCreateRequest,
    WatchlistFilmOut,
    WatchlistItemOut,
    WatchlistListResponse,
)
from upmovies.app.entitlements import require_entitled
from upmovies.app.errors import NotFound, NothingToStop
from upmovies.app.models import User
from upmovies.app.services import watchlist_service
from upmovies.app.services.watchlist_service import WatchlistEntry
from upmovies.deps import get_session, require_csrf

entitled = require_entitled()

router = APIRouter(prefix="/me/watchlist", tags=["me"], dependencies=[Depends(entitled)])


def _to_out(entry: WatchlistEntry) -> WatchlistItemOut:
    """`headline` comes from the service, never off `Film`: the row shows the film's headline
    release (NEU-1397), and `film.release_date` is the primary date the film page does not
    display. `None` — no displayable row and no primary date — renders as "No date yet"."""
    headline = entry.headline
    return WatchlistItemOut(
        film=WatchlistFilmOut(
            id=entry.film.id,
            tmdb_id=entry.film.tmdb_id,
            slug=entry.film.slug,
            title=entry.film.title,
            poster_path=entry.film.poster_path,
            headline_release=(
                None
                if headline is None
                else HeadlineReleaseOut(
                    date=headline.date,
                    kind=headline.kind,
                    country=headline.country,
                    bucket=headline.bucket,
                )
            ),
        ),
        covered_by=[
            WatchlistCoverOut(
                entity_type=cover.entity_type, entity_id=cover.entity_id, name=cover.name
            )
            for cover in entry.covered_by
        ],
        followed=entry.followed,
        muted=entry.muted,
        created_at=entry.created_at,
    )


@router.get("", response_model=WatchlistListResponse)
async def list_watchlist(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> WatchlistListResponse:
    """Every film this user's follows cover, newest first, muted ones included and marked."""
    entries = await watchlist_service.list_items(db, user=user)
    return WatchlistListResponse(items=[_to_out(entry) for entry in entries])


@router.post("", response_model=WatchlistItemOut, dependencies=[Depends(require_csrf)])
async def want_film(
    payload: WatchlistCreateRequest,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> WatchlistItemOut:
    """Want this film: un-mute it, and follow the title if nothing already covers it.

    Always `200` with the resulting item, never `201`. The old split said whether a row had
    been created, which is no longer a question a client can act on — there may be no new row
    at all, and the answer the caller needs is what the film's state *is*."""
    try:
        entry = await watchlist_service.want(db, user=user, film_id=payload.film_id)
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="film_not_found"
        ) from None
    return _to_out(entry)


@router.delete(
    "/{film_id}",
    response_model=WatchlistItemOut,
    responses={204: {"description": "Nothing covers the film any more"}},
    dependencies=[Depends(require_csrf)],
)
async def stop_film(
    film_id: UUID,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> WatchlistItemOut | Response:
    """Stop this film: delete the direct title follow, and mute it if another follow still
    covers it.

    Two answers, because there are two outcomes. `200` with the item when something still
    covers the film — it is on the list, muted, and the client draws it that way. `204` when
    nothing covers it any more: it has left the list, and there is no item to describe.
    `404` for an unknown film, or for one nothing covers and that is not muted — there was
    nothing to stop."""
    try:
        entry = await watchlist_service.stop(db, user=user, film_id=film_id)
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="film_not_found"
        ) from None
    except NothingToStop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="watchlist_item_not_found"
        ) from None
    if entry is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return _to_out(entry)
