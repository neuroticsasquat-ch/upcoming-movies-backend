from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.rate_limit import rate_limit
from upmovies.config import get_settings
from upmovies.deps import get_session
from upmovies.public import service
from upmovies.public.dto import (
    CalendarResponse,
    CollectionDetailResponse,
    CollectionSearchResponse,
    CompanyDetailResponse,
    CompanySearchResponse,
    FeedDayResponse,
    FeedResponse,
    FilmDetailResponse,
    FilmIndexResponse,
    PersonDetailResponse,
    PersonSearchResponse,
    PopularPeopleResponse,
)
from upmovies.public.ical import render_calendar
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


# Entity search stays public rather than behind `require_entitled()` (M3 contracts): it feeds
# the film page's follow buttons and the onboarding grid, both of which render before — and
# regardless of whether — the visitor is entitled. The short-query rule matches /films/search:
# fewer than two alphanumerics is "no query yet" and returns an empty page, not 422.


@router.get("/people/search", response_model=PersonSearchResponse, dependencies=[_public_limit])
async def search_people(
    q: str = Query(..., max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> PersonSearchResponse:
    """Search people by name / original name (case- and accent-insensitive substring),
    most popular first. `id` is the TMDB person id a follow is keyed on."""
    return await service.get_person_search(session, q=q, limit=limit, offset=offset)


@router.get("/people/popular", response_model=PopularPeopleResponse, dependencies=[_public_limit])
async def popular_people(
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> PopularPeopleResponse:
    """The onboarding grid (D-17): the `limit` most popular people who have a profile photo."""
    return await service.get_popular_people(session, limit=limit)


# Registered after the two literal paths above, though it need not be: Starlette matches
# routes in registration order and `/people/search` and `/people/popular` are literals, which
# `{ref}` would happily swallow if it came first. Keeping the order is cheaper than relying on
# it, and `test_person_search_still_routes` is the assertion that it stays true.
@router.get("/people/{ref}", response_model=PersonDetailResponse, dependencies=[_public_limit])
async def get_person(
    ref: str,
    session: AsyncSession = Depends(get_session),
) -> PersonDetailResponse:
    """`ref` is `<person_id>-<name-slug>`, resolved on the leading id. The response's own `ref`
    is the canonical one — callers redirect when it differs from what was requested.

    Public, like the rest of this router and like the entity search beside it: the page renders
    for an anonymous visitor, and the follow control on it is the thing that asks for an
    account. 404 for an id the catalog does not hold and for one TMDB has deleted; the two are
    the same answer because neither is a person anything will be ingested against again.
    """
    person = await service.get_person_detail(session, ref)
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="person not found")
    return person


@router.get("/companies/search", response_model=CompanySearchResponse, dependencies=[_public_limit])
async def search_companies(
    q: str = Query(..., max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> CompanySearchResponse:
    """Search production companies by name (folded substring), alphabetical."""
    return await service.get_company_search(session, q=q, limit=limit, offset=offset)


@router.get(
    "/collections/search", response_model=CollectionSearchResponse, dependencies=[_public_limit]
)
async def search_collections(
    q: str = Query(..., max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> CollectionSearchResponse:
    """Search TMDB collections (franchises) by name (folded substring), alphabetical."""
    return await service.get_collection_search(session, q=q, limit=limit, offset=offset)


# Registered after `/companies/search` and `/collections/search` for the reason spelled above
# `/people/{ref}`: the literal paths would otherwise be swallowed by `{ref}`, and
# `test_entity_search_still_routes` is the assertion that they stay ahead of it.
@router.get("/companies/{ref}", response_model=CompanyDetailResponse, dependencies=[_public_limit])
async def get_company(
    ref: str,
    session: AsyncSession = Depends(get_session),
) -> CompanyDetailResponse:
    """`ref` is `<company_id>-<name-slug>`, resolved on the leading id. The response's own `ref`
    is the canonical one — callers redirect when it differs from what was requested, as the
    person and film pages do. 404 for an id the catalog does not hold."""
    company = await service.get_company_detail(session, ref)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="company not found")
    return company


@router.get(
    "/collections/{ref}", response_model=CollectionDetailResponse, dependencies=[_public_limit]
)
async def get_collection(
    ref: str,
    session: AsyncSession = Depends(get_session),
) -> CollectionDetailResponse:
    """`ref` is `<collection_id>-<name-slug>`, resolved on the leading id. `/companies/{ref}`
    over franchises, down to the redirect rule and the 404."""
    collection = await service.get_collection_detail(session, ref)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection not found")
    return collection


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


# `{token}.ics` rather than a query parameter: a calendar client is handed one URL and asked to
# poll it forever, and the `.ics` suffix is what several of them use to decide the URL is a
# calendar at all before they have seen a response header.
@router.get("/calendar/{token}.ics", dependencies=[_public_limit])
async def get_calendar_feed(
    token: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The subscriber's watchlist release dates as an iCalendar feed (D-34).

    No cookie and no `require_entitled()`: the token *is* the credential, so the gate is applied
    to the token's owner inside the query, and every way of not having a feed — unknown token,
    rotated token, unentitled owner — answers **404**. Not 403: this caller is unauthenticated,
    and a distinguishable refusal would confirm to someone holding a guessed URL that it names a
    real account (D-39). A lapsed subscriber's client therefore keeps the subscription and simply
    stops receiving events, and a renewed grant resumes it on the same URL (D-40).

    `private` in `Cache-Control` because the URL's whole content is one person's watchlist: a
    shared cache holding it would serve one subscriber's films to another. An hour of freshness
    is more than a release date needs — clients poll on their own schedule anyway, and the
    ceiling on how stale this can be is the daily ingest.
    """
    events = await service.get_ical_feed(session, token=token)
    if events is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="calendar not found")
    settings = get_settings()
    return Response(
        content=render_calendar(events, base_url=settings.public_base_url),
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/sitemap.xml")
async def get_sitemap(session: AsyncSession = Depends(get_session)) -> Response:
    settings = get_settings()
    films = await service.get_sitemap_films(session)
    entities = await service.get_sitemap_entities(session)
    return Response(
        content=render_sitemap(settings.public_base_url, films, entities),
        media_type="application/xml",
    )
