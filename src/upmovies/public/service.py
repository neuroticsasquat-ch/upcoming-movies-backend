from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Date, cast, distinct, func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.public.arc import derive_arc_stage, most_significant_event_type
from upmovies.public.dto import (
    EventOut,
    FeedDayItem,
    FeedDayResponse,
    FeedItem,
    FeedResponse,
    FilmDetailResponse,
    FilmIndexItem,
    FilmIndexResponse,
    SourceOut,
)
from upmovies.public.sources import cap_sources, outlet_label

_HIDDEN_EVENT_TYPES = ("other",)


def _visible_events():
    """SQL predicate: an event reaches the public surface unless its type is hidden.

    `other` is the uncategorized catch-all where residual hype lands (NEU-367); it is
    hidden from users but kept in the table (reversible)."""
    return Event.event_type.notin_(_HIDDEN_EVENT_TYPES)


def _release_year(release_date: date | None) -> int | None:
    return release_date.year if release_date is not None else None


async def _event_types_by_film(
    session: AsyncSession, film_ids: list[UUID]
) -> dict[UUID, list[str]]:
    if not film_ids:
        return {}
    rows = await session.execute(
        select(Event.film_id, Event.event_type).where(Event.film_id.in_(film_ids)).distinct()
    )
    result: dict[UUID, list[str]] = {}
    for film_id, event_type in rows:
        result.setdefault(film_id, []).append(event_type)
    return result


async def get_film_index(session: AsyncSession, *, limit: int, offset: int) -> FilmIndexResponse:
    has_summary = (
        select(Event.id)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .where(Event.film_id == Film.id, _visible_events())
        .exists()
    )
    total = await session.scalar(
        select(func.count()).select_from(Film).where(Film.slug.is_not(None), has_summary)
    )
    films = (
        (
            await session.execute(
                select(Film)
                .where(Film.slug.is_not(None), has_summary)
                .order_by(nulls_last(Film.release_date.desc()), Film.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    event_types = await _event_types_by_film(session, [film.id for film in films])
    items: list[FilmIndexItem] = []
    for film in films:
        assert film.slug is not None
        items.append(
            FilmIndexItem(
                slug=film.slug,
                title=film.title,
                release_year=_release_year(film.release_date),
                poster_path=film.poster_path,
                arc_stage=derive_arc_stage(film.status, event_types.get(film.id, [])),
            )
        )
    return FilmIndexResponse(items=items, total=total or 0, limit=limit, offset=offset)


async def get_film_detail(session: AsyncSession, slug: str) -> FilmDetailResponse | None:
    film = (await session.execute(select(Film).where(Film.slug == slug))).scalar_one_or_none()
    if film is None:
        return None
    assert film.slug is not None

    all_event_types = [
        event_type
        for (event_type,) in await session.execute(
            select(Event.event_type).where(Event.film_id == film.id).distinct()
        )
    ]
    arc_stage = derive_arc_stage(film.status, all_event_types)

    summarized = (
        await session.execute(
            select(Event, EventSummary.summary)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .where(Event.film_id == film.id, _visible_events())
            .order_by(Event.occurred_at.asc(), Event.id.asc())
        )
    ).all()

    event_ids = [event.id for event, _ in summarized]
    sources_by_event: dict[UUID, list[Story]] = {}
    if event_ids:
        source_rows = (
            await session.execute(
                select(EventStory.event_id, Story)
                .join(Story, Story.id == EventStory.story_id)
                .where(EventStory.event_id.in_(event_ids))
                .order_by(nulls_last(Story.published_at.asc()), Story.id.asc())
            )
        ).all()
        for event_id, story in source_rows:
            sources_by_event.setdefault(event_id, []).append(story)

    events = [
        EventOut(
            event_type=event.event_type,
            confidence=event.confidence,
            occurred_at=event.occurred_at,
            summary=summary,
            sources=[
                SourceOut(
                    url=story.url,
                    source=outlet_label(story),
                    title=story.title,
                    published_at=story.published_at,
                )
                for story in cap_sources(sources_by_event.get(event.id, []))
            ],
        )
        for event, summary in summarized
    ]
    return FilmDetailResponse(
        slug=film.slug,
        title=film.title,
        release_date=film.release_date,
        release_year=_release_year(film.release_date),
        poster_path=film.poster_path,
        arc_stage=arc_stage,
        events=events,
    )


@dataclass
class SitemapFilm:
    slug: str
    lastmod: datetime


async def get_sitemap_films(session: AsyncSession) -> list[SitemapFilm]:
    rows = (
        await session.execute(
            select(Film.slug, func.max(Event.created_at))
            .join(Event, Event.film_id == Film.id)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .where(_visible_events())
            .group_by(Film.id, Film.slug)
            .order_by(Film.slug.asc())
        )
    ).all()
    return [SitemapFilm(slug=slug, lastmod=last_event_created) for slug, last_event_created in rows]


async def get_feed(session: AsyncSession, *, limit: int, offset: int) -> FeedResponse:
    total = await session.scalar(
        select(func.count())
        .select_from(Event)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .join(Film, Film.id == Event.film_id)
        .where(Film.slug.is_not(None), _visible_events())
    )
    rows = (
        await session.execute(
            select(Event, EventSummary.summary, Film.slug, Film.title)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(Film.slug.is_not(None), _visible_events())
            .order_by(Event.created_at.desc(), Event.id.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    event_ids = [event.id for event, _summary, _slug, _title in rows]
    sources_by_event: dict[UUID, list[Story]] = {}
    if event_ids:
        source_rows = (
            await session.execute(
                select(EventStory.event_id, Story)
                .join(Story, Story.id == EventStory.story_id)
                .where(EventStory.event_id.in_(event_ids))
                .order_by(nulls_last(Story.published_at.asc()), Story.id.asc())
            )
        ).all()
        for event_id, story in source_rows:
            sources_by_event.setdefault(event_id, []).append(story)

    items: list[FeedItem] = []
    for event, summary, slug, title in rows:
        assert slug is not None
        items.append(
            FeedItem(
                film_slug=slug,
                film_title=title,
                event_type=event.event_type,
                confidence=event.confidence,
                occurred_at=event.occurred_at,
                created_at=event.created_at,
                summary=summary,
                sources=[
                    SourceOut(
                        url=story.url,
                        source=outlet_label(story),
                        title=story.title,
                        published_at=story.published_at,
                    )
                    for story in cap_sources(sources_by_event.get(event.id, []))
                ],
            )
        )
    return FeedResponse(items=items, total=total or 0, limit=limit, offset=offset)


async def get_feed_grouped(session: AsyncSession, *, limit: int, offset: int) -> FeedDayResponse:
    day = cast(func.timezone("UTC", Event.created_at), Date)
    last_created = func.max(Event.created_at)

    base = (
        select(
            Film.slug.label("slug"),
            Film.title.label("title"),
            Film.poster_path.label("poster_path"),
            day.label("day"),
            func.count().label("event_count"),
            func.array_agg(distinct(Event.event_type)).label("event_types"),
        )
        .select_from(Event)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .join(Film, Film.id == Event.film_id)
        .where(Film.slug.is_not(None), _visible_events())
        .group_by(Film.id, Film.slug, Film.title, Film.poster_path, day)
    )

    total = await session.scalar(select(func.count()).select_from(base.subquery()))

    rows = (
        await session.execute(
            base.order_by(day.desc(), last_created.desc(), Film.slug.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    items = [
        FeedDayItem(
            film_slug=slug,
            film_title=title,
            poster_path=poster_path,
            day=day_value,
            top_event_type=most_significant_event_type(event_types),
            event_count=event_count,
        )
        for slug, title, poster_path, day_value, event_count, event_types in rows
    ]
    return FeedDayResponse(items=items, total=total or 0, limit=limit, offset=offset)
