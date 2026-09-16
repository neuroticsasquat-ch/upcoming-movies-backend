from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.rate_limit import rate_limit
from upmovies.config import get_settings
from upmovies.deps import get_session
from upmovies.public import service
from upmovies.public.dto import (
    CalendarResponse,
    FeedDayResponse,
    FeedResponse,
    FilmDetailResponse,
    FilmIndexResponse,
)
from upmovies.public.sitemap import render_sitemap

router = APIRouter(tags=["public"])

# Every read the anonymous site makes shares one bucket (spec §2). `/sitemap.xml` is
# deliberately not in it: it is fetched by crawlers, cached upstream, and metering it would
# throttle indexing rather than abuse. The bucket is inert until `RATE_LIMIT_PUBLIC_ENABLED`
# is set — see `app/rate_limit.py` for why it ships off.
_public_limit = Depends(rate_limit("public"))


@router.get("/films/search", response_model=FilmIndexResponse, dependencies=[_public_limit])
async def search_films(
    q: str = Query(..., max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FilmIndexResponse:
    """Search the whole film catalog by title / original title (case-insensitive substring).

    Unlike the /films index and /feed (which surface only films with a visible, summarized
    news event), search covers every slugged film — including ones with no news yet and ones
    that are not upcoming — so the full catalog is reachable by title.

    Queries with fewer than two alphanumeric characters (blank, single-character, or
    all-punctuation, e.g. ``%``) intentionally return an empty page (``items: []``,
    ``total: 0``) rather than 422 -- they are treated as "no query yet", not an error.
    """
    return await service.get_film_search(session, q=q, limit=limit, offset=offset)


@router.get("/films/{ref}", response_model=FilmDetailResponse, dependencies=[_public_limit])
async def get_film(
    ref: str,
    session: AsyncSession = Depends(get_session),
) -> FilmDetailResponse:
    """`ref` is `<tmdb_id>-<title-slug>`, resolved on the leading id; a legacy `film.slug` still
    resolves. The response's own `ref` is the canonical one — callers redirect when it differs
    from what was requested."""
    film = await service.get_film_detail(session, ref)
    if film is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="film not found")
    return film


@router.get("/feed", response_model=FeedResponse, dependencies=[_public_limit])
async def get_feed(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FeedResponse:
    return await service.get_feed(session, limit=limit, offset=offset)


@router.get("/feed/grouped", response_model=FeedDayResponse, dependencies=[_public_limit])
async def get_grouped_feed(
    # limit/offset count distinct days (newest first), not film rows.
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FeedDayResponse:
    return await service.get_feed_grouped(session, limit=limit, offset=offset)


@router.get("/calendar", response_model=CalendarResponse, dependencies=[_public_limit])
async def get_calendar(
    # limit/offset count distinct release dates (soonest first), not film rows.
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> CalendarResponse:
    return await service.get_calendar(session, limit=limit, offset=offset)


@router.get("/sitemap.xml")
async def get_sitemap(session: AsyncSession = Depends(get_session)) -> Response:
    settings = get_settings()
    films = await service.get_sitemap_films(session)
    return Response(
        content=render_sitemap(settings.public_base_url, films),
        media_type="application/xml",
    )
