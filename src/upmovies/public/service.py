import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Date,
    case,
    cast,
    distinct,
    exists,
    func,
    nulls_last,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import (
    Collection,
    Film,
    FilmAlternativeTitle,
    FilmCredit,
    FilmGenre,
    FilmProductionCompany,
    FilmReleaseDate,
    Genre,
    Person,
    ProductionCompany,
)
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.public.arc import derive_arc_stage, most_significant_event_type
from upmovies.public.dto import (
    CalendarItem,
    CalendarResponse,
    CastMemberOut,
    CollectionOut,
    EventOut,
    FeedDayItem,
    FeedDayResponse,
    FeedItem,
    FeedResponse,
    FilmDetailResponse,
    FilmIndexItem,
    FilmIndexResponse,
    ReleaseDateOut,
    SourceOut,
)
from upmovies.public.release import _TMDB_TYPE_TO_BUCKET, release_label_for_tmdb_type
from upmovies.public.sources import cap_sources, outlet_label

_HIDDEN_EVENT_TYPES = ("other",)

MIN_QUERY_LEN = 2

CALENDAR_REGION = "US"  # single governing region for v1


def _visible_events() -> ColumnElement[bool]:
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


def _publicly_visible_film() -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """Return a tuple of WHERE clauses that restrict a Film query to publicly visible films."""
    has_summary = (
        select(Event.id)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .where(Event.film_id == Film.id, _visible_events())
        .exists()
    )
    return Film.slug.is_not(None), has_summary


async def _film_index_items(session: AsyncSession, films: list[Film]) -> list[FilmIndexItem]:
    """Build FilmIndexItem list for a page of Film rows."""
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
    return items


def _build_diacritic_maps() -> tuple[str, str]:
    """Build translate() from/to strings mapping each lowercase Latin letter that carries a
    diacritic to its base ASCII letter (é→e, ō→o, ñ→n, …). Covers the precomposed singles in
    Latin-1 Supplement + Latin Extended-A/B; multi-char folds (æ, ß) are left untouched."""
    frm: list[str] = []
    to: list[str] = []
    seen: set[str] = set()
    for cp in range(0x00C0, 0x0250):
        ch = chr(cp)
        if not ch.isalpha():
            continue
        base = unicodedata.normalize("NFD", ch)[0]
        if not (base.isascii() and base.isalpha()) or base == ch:
            continue
        low = ch.lower()
        if len(low) != 1 or low in seen:
            continue
        seen.add(low)
        frm.append(low)
        to.append(base.lower())
    return "".join(frm), "".join(to)


_DIACRITIC_FROM, _DIACRITIC_TO = _build_diacritic_maps()
_PY_DIACRITIC = str.maketrans(_DIACRITIC_FROM, _DIACRITIC_TO)


def _normalized_col(col: Any) -> ColumnElement[str]:
    """SQL-side fold for fuzzy title matching: lowercase, strip diacritics, then drop every
    non-alphanumeric character — so 'Spider-Man' / 'Shōgun' compare as 'spiderman' / 'shogun'.
    `[:alnum:]` is Unicode-aware in the UTF-8 DB, so non-Latin titles (e.g. '기생충') survive."""
    return func.regexp_replace(
        func.translate(func.lower(col), _DIACRITIC_FROM, _DIACRITIC_TO),
        "[^[:alnum:]]",
        "",
        "g",
    )


def _normalize_query(q: str) -> str:
    """Python-side counterpart of _normalized_col, applied to the user's query. Mirrors the
    SQL fold exactly (same diacritic map + keep-alphanumerics) so both sides agree across
    scripts."""
    folded = q.lower().translate(_PY_DIACRITIC)
    return "".join(c for c in folded if c.isalnum())


def _primary_title_match(nq: str) -> ColumnElement[bool]:
    """Match the normalized query against the film's primary title or original_title."""
    pattern = f"%{nq}%"
    return or_(
        _normalized_col(Film.title).like(pattern),
        _normalized_col(Film.original_title).like(pattern),
    )


def _title_match(nq: str) -> ColumnElement[bool]:
    """Boolean clause matching the normalized query against title/original_title/alt-titles.

    Alt-title matching uses a correlated EXISTS subquery so each film appears at most once
    (no DISTINCT needed). The fold (lowercase + de-accent + strip non-alphanumerics) is
    applied to both the query and each column so 'spiderman' / 'spider man' find 'Spider-Man'.
    FUTURE: the fold is non-sargable; a functional pg_trgm index on _normalized_col would let
    this skip the sequential scan if search gets hot.
    """
    pattern = f"%{nq}%"
    alt_title_match = exists(
        select(1).where(
            FilmAlternativeTitle.film_id == Film.id,
            _normalized_col(FilmAlternativeTitle.title).like(pattern),
        )
    )
    return or_(_primary_title_match(nq), alt_title_match)


async def get_film_search(
    session: AsyncSession, *, q: str, limit: int, offset: int
) -> FilmIndexResponse:
    term = q.strip()
    # Gate on alphanumeric count, not raw length: require at least MIN_QUERY_LEN
    # alphanumeric characters. One check short-circuits blank/whitespace, single-
    # character, and all-punctuation queries (e.g. "", "a", "%", "_", "--") to an
    # empty page instead of running an unbounded %term% scan. This also gates the
    # wildcard-literal path: "%"/"_" have zero alphanumerics, so they return empty
    # here -- _escape_like / the wildcard-literal tests only exercise escaping for
    # queries that clear this gate (e.g. "50%", which has two alphanumerics).
    alphanumeric_len = sum(1 for c in term if c.isalnum())
    if alphanumeric_len < MIN_QUERY_LEN:
        return FilmIndexResponse(items=[], total=0, limit=limit, offset=offset)
    nq = _normalize_query(term)
    where = (*_publicly_visible_film(), _title_match(nq))
    total = await session.scalar(select(func.count()).select_from(Film).where(*where))
    films = (
        (
            await session.execute(
                select(Film)
                .where(*where)
                .order_by(
                    # case() avoids NULL from original_title IS NULL sorting first under DESC.
                    case((_primary_title_match(nq), 1), else_=0).desc(),
                    nulls_last(Film.release_date.desc()),
                    Film.id.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    items = await _film_index_items(session, list(films))
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
            .order_by(Event.created_at.asc(), Event.id.asc())
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
        for event, summary in summarized
    ]

    regions: set[str] = {"US"}
    if film.origin_country:
        regions.add(film.origin_country[0])

    release_date_rows = (
        (
            await session.execute(
                select(FilmReleaseDate)
                .where(
                    FilmReleaseDate.film_id == film.id,
                    FilmReleaseDate.iso_3166_1.in_(regions),
                )
                .order_by(
                    FilmReleaseDate.release_date.asc(),
                    FilmReleaseDate.release_type.asc(),
                    FilmReleaseDate.iso_3166_1.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    # Surface only the theatrical arc (wide + limited); premiere/digital/physical/TV are dropped.
    release_dates = [
        ReleaseDateOut(
            country=row.iso_3166_1,
            release_type=row.release_type,
            type_label=label,
            date=row.release_date,
            certification=row.certification,
        )
        for row in release_date_rows
        if (label := release_label_for_tmdb_type(row.release_type)) is not None
    ]

    genres = list(
        (
            await session.execute(
                select(Genre.name)
                .join(FilmGenre, FilmGenre.genre_id == Genre.id)
                .where(FilmGenre.film_id == film.id)
                .order_by(Genre.name.asc(), Genre.id.asc())
            )
        )
        .scalars()
        .all()
    )

    companies = list(
        (
            await session.execute(
                select(ProductionCompany.name)
                .join(
                    FilmProductionCompany, FilmProductionCompany.company_id == ProductionCompany.id
                )
                .where(FilmProductionCompany.film_id == film.id)
                .order_by(ProductionCompany.name.asc(), ProductionCompany.id.asc())
            )
        )
        .scalars()
        .all()
    )

    collection: CollectionOut | None = None
    if film.collection_id is not None:
        col_row = (
            await session.execute(select(Collection).where(Collection.id == film.collection_id))
        ).scalar_one_or_none()
        if col_row is not None:
            collection = CollectionOut(name=col_row.name, poster_path=col_row.poster_path)

    _excluded_titles = {t.lower() for t in [film.title, film.original_title] if t}
    _alt_title_rows = list(
        (
            await session.execute(
                select(FilmAlternativeTitle.title).where(
                    FilmAlternativeTitle.film_id == film.id,
                    func.lower(FilmAlternativeTitle.title).notin_(_excluded_titles),
                )
            )
        )
        .scalars()
        .all()
    )
    # Deduplicate case-insensitively, order alphabetically, cap at 8.
    _seen: set[str] = set()
    _deduped: list[str] = []
    for _t in _alt_title_rows:
        if _t.lower() not in _seen:
            _seen.add(_t.lower())
            _deduped.append(_t)
    alternative_titles = sorted(_deduped, key=str.lower)[:8]

    cast_rows = (
        await session.execute(
            select(Person.name, FilmCredit.character, Person.profile_path)
            .join(FilmCredit, FilmCredit.person_id == Person.id)
            .where(FilmCredit.film_id == film.id, FilmCredit.credit_type == "cast")
            .order_by(nulls_last(FilmCredit.credit_order.asc()), Person.name.asc())
            .limit(12)
        )
    ).all()
    cast_out = [
        CastMemberOut(name=r.name, character=r.character, profile_path=r.profile_path)
        for r in cast_rows
    ]

    director_rows = (
        await session.execute(
            select(Person.name)
            .join(FilmCredit, FilmCredit.person_id == Person.id)
            .where(
                FilmCredit.film_id == film.id,
                FilmCredit.credit_type == "crew",
                FilmCredit.job == "Director",
            )
            .order_by(nulls_last(FilmCredit.credit_order.asc()), Person.name.asc())
        )
    ).all()
    directors_out = [r.name for r in director_rows]

    return FilmDetailResponse(
        slug=film.slug,
        title=film.title,
        tmdb_id=film.tmdb_id,
        imdb_id=film.imdb_id,
        release_date=film.release_date,
        release_year=_release_year(film.release_date),
        poster_path=film.poster_path,
        arc_stage=arc_stage,
        events=events,
        release_dates=release_dates,
        overview=film.overview,
        tagline=film.tagline,
        runtime=film.runtime,
        vote_average=film.vote_average,
        vote_count=film.vote_count,
        original_language=film.original_language,
        backdrop_path=film.backdrop_path,
        genres=genres,
        production_companies=companies,
        collection=collection,
        alternative_titles=alternative_titles,
        cast=cast_out,
        directors=directors_out,
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
    # Pagination is by DAY: limit/offset count distinct days (newest first), not film rows —
    # so the UI shows "N days at a time" with a deterministic "view more". `total` is the
    # number of distinct days, so the client knows when no more days remain.
    day = cast(func.timezone("UTC", Event.created_at), Date)
    visible = (Film.slug.is_not(None), _visible_events())

    distinct_days = (
        select(day.label("day"))
        .select_from(Event)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .join(Film, Film.id == Event.film_id)
        .where(*visible)
        .group_by(day)
    )
    total_days = await session.scalar(select(func.count()).select_from(distinct_days.subquery()))

    window = distinct_days.order_by(day.desc()).limit(limit).offset(offset).subquery()

    rows = (
        await session.execute(
            select(
                Film.slug.label("slug"),
                Film.title.label("title"),
                Film.release_date.label("release_date"),
                Film.poster_path.label("poster_path"),
                day.label("day"),
                func.count().label("event_count"),
                func.array_agg(distinct(Event.event_type)).label("event_types"),
            )
            .select_from(Event)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(*visible, day.in_(select(window.c.day)))
            .group_by(Film.id, Film.slug, Film.title, Film.release_date, Film.poster_path, day)
            .order_by(day.desc(), nulls_last(Film.popularity.desc()), Film.slug.asc())
        )
    ).all()

    items = [
        FeedDayItem(
            film_slug=slug,
            film_title=title,
            release_year=_release_year(release_date),
            poster_path=poster_path,
            day=day_value,
            top_event_type=most_significant_event_type(event_types),
            event_count=event_count,
        )
        for slug, title, release_date, poster_path, day_value, event_count, event_types in rows
    ]
    return FeedDayResponse(items=items, total=total_days or 0, limit=limit, offset=offset)


async def get_calendar(session: AsyncSession, *, limit: int, offset: int) -> CalendarResponse:
    today = datetime.now(tz=UTC).date()  # Python-side, NOT SQL CURRENT_DATE
    rel_day = cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)
    surfaced_types = tuple(_TMDB_TYPE_TO_BUCKET)  # (1, 2, 3) — derived, never drifts

    # Pagination is by DATE: limit/offset count distinct release dates (soonest first), not
    # film rows — so the UI shows "N dates at a time" with a deterministic "view more".
    # `total` is the number of distinct upcoming dates.
    visible = (
        FilmReleaseDate.iso_3166_1 == CALENDAR_REGION,
        FilmReleaseDate.release_type.in_(surfaced_types),
        FilmReleaseDate.release_date >= datetime(today.year, today.month, today.day, tzinfo=UTC),
        Film.slug.is_not(None),
        func.coalesce(Film.adult, False).is_(False),
    )

    distinct_dates = (
        select(rel_day.label("d"))
        .select_from(FilmReleaseDate)
        .join(Film, Film.id == FilmReleaseDate.film_id)
        .where(*visible)
        .group_by(rel_day)
    )
    total = await session.scalar(select(func.count()).select_from(distinct_dates.subquery()))

    window = distinct_dates.order_by(rel_day.asc()).limit(limit).offset(offset).subquery()

    rows = (
        await session.execute(
            select(
                Film.slug.label("slug"),
                Film.title.label("title"),
                Film.release_date.label("film_release_date"),
                Film.poster_path.label("poster_path"),
                rel_day.label("release_date"),
                FilmReleaseDate.release_type.label("release_type"),
            )
            .select_from(FilmReleaseDate)
            .join(Film, Film.id == FilmReleaseDate.film_id)
            .where(*visible, rel_day.in_(select(window.c.d)))
            .group_by(
                Film.id,
                Film.slug,
                Film.title,
                Film.release_date,
                Film.poster_path,
                rel_day,
                FilmReleaseDate.release_type,
            )
            # Within a date, wide (3) before limited (2) → release_type DESC.
            .order_by(
                rel_day.asc(),
                FilmReleaseDate.release_type.desc(),
                nulls_last(Film.popularity.desc()),
                Film.slug.asc(),
            )
        )
    ).all()

    items = [
        CalendarItem(
            film_slug=slug,
            film_title=title,
            release_year=_release_year(film_release_date),
            poster_path=poster_path,
            release_date=release_date,
            release_type=_TMDB_TYPE_TO_BUCKET[release_type],
        )
        for slug, title, film_release_date, poster_path, release_date, release_type in rows
    ]
    return CalendarResponse(items=items, total=total or 0, limit=limit, offset=offset)
