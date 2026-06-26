from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.config import get_settings
from upmovies.deps import get_session
from upmovies.public import service
from upmovies.public.dto import (
    FeedDayResponse,
    FeedResponse,
    FilmDetailResponse,
    FilmIndexResponse,
)
from upmovies.public.sitemap import render_sitemap

router = APIRouter(tags=["public"])


@router.get("/films", response_model=FilmIndexResponse)
async def list_films(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FilmIndexResponse:
    return await service.get_film_index(session, limit=limit, offset=offset)


@router.get("/films/search", response_model=FilmIndexResponse)
async def search_films(
    q: str = Query(..., max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FilmIndexResponse:
    """Search publicly-visible films by title / original title (case-insensitive substring).

    Queries with fewer than two alphanumeric characters (blank, single-character, or
    all-punctuation, e.g. ``%``) intentionally return an empty page (``items: []``,
    ``total: 0``) rather than 422 -- they are treated as "no query yet", not an error.
    """
    return await service.get_film_search(session, q=q, limit=limit, offset=offset)


@router.get("/films/{slug}", response_model=FilmDetailResponse)
async def get_film(
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> FilmDetailResponse:
    film = await service.get_film_detail(session, slug)
    if film is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="film not found")
    return film


@router.get("/feed", response_model=FeedResponse)
async def get_feed(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FeedResponse:
    return await service.get_feed(session, limit=limit, offset=offset)


@router.get("/feed/grouped", response_model=FeedDayResponse)
async def get_grouped_feed(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> FeedDayResponse:
    return await service.get_feed_grouped(session, limit=limit, offset=offset)


@router.get("/sitemap.xml")
async def get_sitemap(session: AsyncSession = Depends(get_session)) -> Response:
    settings = get_settings()
    films = await service.get_sitemap_films(session)
    return Response(
        content=render_sitemap(settings.public_base_url, films),
        media_type="application/xml",
    )
