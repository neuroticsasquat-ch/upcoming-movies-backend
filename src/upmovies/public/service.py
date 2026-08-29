import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Date,
    any_,
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
from upmovies.catalog.ref import film_ref, parse_film_ref
from upmovies.catalog.release_grade import (
    PRIMARY_REGION,
    RELEASE_TYPE_BUCKETS,
    displayable_regions,
)
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.news.visibility import visible_events
from upmovies.public.arc import (
    derive_arc_stage,
    most_significant_event_type,
    ordered_event_types,
)
from upmovies.public.dto import (
    CalendarItem,
    CalendarResponse,
    CastMemberOut,
    CollectionOut,
    CrewMemberOut,
    DayGroup,
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
from upmovies.public.release import release_label_for_tmdb_type
from upmovies.public.sources import cap_sources, outlet_label, source_url

MIN_QUERY_LEN = 2

CALENDAR_REGION = "US"  # single governing region for v1

_CREW_DEPARTMENT_ORDER = (
    "Directing",
    "Writing",
    "Production",
    "Camera",
    "Editing",
    "Sound",
    "Art",
    "Costume & Make-Up",
    "Visual Effects",
    "Lighting",
    "Crew",
)
_DEPT_PRIORITY = {name: i for i, name in enumerate(_CREW_DEPARTMENT_ORDER)}
_DEPT_UNKNOWN = len(_CREW_DEPARTMENT_ORDER)


def _crew_sort_key(row: Any) -> tuple:
    """Order crew by department priority, then job (alpha), then credit_order (nulls last),
    then name. Unknown departments sort after all known ones, alphabetically by name."""
    dept = row.department or ""
    return (
        _DEPT_PRIORITY.get(dept, _DEPT_UNKNOWN),
        dept,
        row.job or "",
        row.credit_order is None,
        0 if row.credit_order is None else row.credit_order,
        row.name,
    )


def _natural_title_col() -> ColumnElement[str]:
    """SQL expression for natural English title sort: strip leading 'A ', 'An ', 'The ' (case-
    insensitive) before comparing. Non-matching titles sort unchanged, so 'Batman' sorts
    before 'The Batman' as 'Batman' vs 'Batman' — the second word decides."""
    title = func.lower(Film.title)
    return func.regexp_replace(title, r"^(a|an|the)\s+", "", "i")


def _region_visible() -> ColumnElement[bool]:
    """SQL predicate: a release_date event reaches the public surface only when its region is
    global (NULL) or in the film's primary set — US plus the film's origin countries. Other
    event types are never region-filtered. Requires Film to be present in the query (NEU-446)."""
    return or_(
        Event.event_type != "release_date",
        Event.region.is_(None),
        Event.region == PRIMARY_REGION,
        Event.region == any_(Film.origin_country),
    )


def _has_story() -> ColumnElement[bool]:
    """SQL predicate: this event has picked up at least one story. The news-backed classifier
    (NEU-1137) — deliberately not `Event.provenance == "story"`, which records where the event
    was *born* and is never mutated when a story attaches to it later.

    A correlated EXISTS rather than a join to `event_story`, because the grouped feed already
    aggregates per (film, day): joining would multiply a multi-source event's row and inflate
    `event_count`.

    `correlate(Event)` is explicit rather than left to SQLAlchemy's auto-correlation: embedded
    somewhere `Event` is not already in the enclosing FROM, this would silently widen into
    "does *any* event anywhere have a story" — true for every row, and a wrong answer rather
    than an error.
    """
    return exists(select(1).where(EventStory.event_id == Event.id).correlate(Event))


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _release_year(release_date: date | None) -> int | None:
    return release_date.year if release_date is not None else None


def _day_heading(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]}, {_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _film_index_items(films: list[Film]) -> list[FilmIndexItem]:
    """Build FilmIndexItem list for a page of Film rows."""
    items: list[FilmIndexItem] = []
    for film in films:
        items.append(
            FilmIndexItem(
                ref=film_ref(film.tmdb_id, film.title),
                title=film.title,
                release_year=_release_year(film.release_date),
                poster_path=film.poster_path,
                arc_stage=derive_arc_stage(film.status),
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
    # Search spans the whole catalog: any slugged film whose title matches, regardless of
    # whether it has news events yet or is upcoming. This is deliberately broader than the
    # /films index and /feed, which gate on a visible, summarized event. The slug guard stays
    # as the "is this film public at all" marker: a film without one was never published, and
    # its URL ref is now built from tmdb_id + title rather than read from the column.
    where = (Film.slug.is_not(None), _title_match(nq))
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
    items = _film_index_items(list(films))
    return FilmIndexResponse(items=items, total=total or 0, limit=limit, offset=offset)


async def get_film_detail(session: AsyncSession, ref: str) -> FilmDetailResponse | None:
    """Resolve a film by URL ref (`<tmdb_id>-<title-slug>`), falling back to the legacy immutable
    `film.slug` for URLs minted before NEU-1143.

    Both candidates go in one query, and an exact slug match wins. They can genuinely collide: a
    numeric title slugs to something that reads as a ref — the film "1917" is slugged `1917-2019`
    and parses as id 1917, a different real film. Preferring the slug points the ambiguous string
    at the URL that was actually minted for it, which is the one search engines already hold.
    """
    tmdb_id = parse_film_ref(ref)
    where = Film.slug == ref if tmdb_id is None else or_(Film.slug == ref, Film.tmdb_id == tmdb_id)
    candidates = (await session.execute(select(Film).where(where))).scalars().all()
    film = next((c for c in candidates if c.slug == ref), None) or next(iter(candidates), None)
    if film is None:
        return None

    arc_stage = derive_arc_stage(film.status)

    summarized = (
        await session.execute(
            select(
                Event,
                EventSummary.summary,
                EventSummary.edited_at,
                _has_story().label("has_story"),
            )
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(Event.film_id == film.id, visible_events(), _region_visible())
            .order_by(Event.occurred_at.asc(), Event.created_at.asc(), Event.id.asc())
        )
    ).all()

    event_ids = [event.id for event, _summary, _edited_at, _has_story in summarized]
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

    def _to_event_out(event: Any, summary: str | None, edited_at: datetime | None) -> EventOut:
        return EventOut(
            event_id=event.id,
            event_type=event.event_type,
            confidence=event.confidence,
            created_at=event.created_at,
            summary=summary,  # type: ignore  — guaranteed non-null by visible_events() filter
            summary_edited=edited_at is not None,
            provenance=event.provenance,
            sources=[
                SourceOut(
                    url=source_url(story),
                    source=outlet_label(story),
                    title=story.title,
                    published_at=story.published_at,
                )
                for story in cap_sources(sources_by_event.get(event.id, []))
            ],
        )

    # Group events by UTC day key, split by has_story (NEU-1201).
    day_groups: list[DayGroup] = []
    day_events: dict[date, tuple[list[EventOut], list[EventOut]]] = {}
    for event, summary, edited_at, has_story in summarized:
        utc = event.occurred_at.astimezone(UTC)
        day_key = date(utc.year, utc.month, utc.day)
        eout = _to_event_out(event, summary, edited_at)
        news_list, tmdb_list = day_events.setdefault(day_key, ([], []))
        (news_list if has_story else tmdb_list).append(eout)
    for day_key in sorted(day_events, reverse=True):
        news, tmdb = day_events[day_key]
        day_groups.append(
            DayGroup(
                day=day_key,
                heading=_day_heading(day_key),
                news_events=news,
                tmdb_events=tmdb,
            )
        )

    # The one definition, shared with the event writer and with `_region_visible` above
    # (`catalog.release_grade`). This used to take `origin_country[0]` while the visibility
    # predicate took all of them, so a co-production could surface an event about a date the
    # page declined to list — the drift NEU-1121 closes.
    regions = displayable_regions(film.origin_country)

    # Governing release date: one row per (country, category) subject, the earliest
    # displayable date (NEU-1206). Ties break by FilmReleaseDate.id for stability.
    release_date_rows = (
        (
            await session.execute(
                select(FilmReleaseDate)
                .distinct(FilmReleaseDate.iso_3166_1, FilmReleaseDate.release_type)
                .where(
                    FilmReleaseDate.film_id == film.id,
                    FilmReleaseDate.iso_3166_1.in_(regions),
                )
                .order_by(
                    FilmReleaseDate.iso_3166_1.asc(),
                    FilmReleaseDate.release_type.asc(),
                    FilmReleaseDate.release_date.asc(),
                    FilmReleaseDate.id.asc(),
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

    # If no displayable release dates remain after filtering but the film has a primary
    # release_date, fall back to that date without a country code.
    if not release_dates and film.release_date is not None:
        release_dates.append(
            ReleaseDateOut(
                country="",
                release_type=0,
                type_label="",
                date=datetime.combine(film.release_date, datetime.min.time(), tzinfo=UTC),
                certification=None,
            )
        )

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

    crew_rows = (
        await session.execute(
            select(
                Person.name,
                FilmCredit.job,
                FilmCredit.department,
                FilmCredit.credit_order,
            )
            .join(FilmCredit, FilmCredit.person_id == Person.id)
            .where(FilmCredit.film_id == film.id, FilmCredit.credit_type == "crew")
        )
    ).all()
    crew_out = [
        CrewMemberOut(name=r.name, job=r.job, department=r.department)
        for r in sorted(crew_rows, key=_crew_sort_key)
    ]

    return FilmDetailResponse(
        ref=film_ref(film.tmdb_id, film.title),
        title=film.title,
        tmdb_id=film.tmdb_id,
        imdb_id=film.imdb_id,
        release_date=film.release_date,
        release_year=_release_year(film.release_date),
        poster_path=film.poster_path,
        arc_stage=arc_stage,
        day_groups=day_groups,
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
        crew=crew_out,
    )


@dataclass
class SitemapFilm:
    ref: str
    lastmod: datetime


async def get_sitemap_films(session: AsyncSession) -> list[SitemapFilm]:
    rows = (
        await session.execute(
            select(Film.tmdb_id, Film.title, func.max(Event.created_at))
            .join(Event, Event.film_id == Film.id)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .where(visible_events(), _region_visible())
            .group_by(Film.id, Film.tmdb_id, Film.title)
            .order_by(Film.slug.asc())
        )
    ).all()
    return [
        SitemapFilm(ref=film_ref(tmdb_id, title), lastmod=lastmod)
        for tmdb_id, title, lastmod in rows
    ]


async def get_feed(session: AsyncSession, *, limit: int, offset: int) -> FeedResponse:
    total = await session.scalar(
        select(func.count())
        .select_from(Event)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .join(Film, Film.id == Event.film_id)
        .where(Film.slug.is_not(None), visible_events(), _region_visible())
    )
    rows = (
        await session.execute(
            select(Event, EventSummary.summary, Film.tmdb_id, Film.title)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(Film.slug.is_not(None), visible_events(), _region_visible())
            .order_by(Event.created_at.desc(), Event.id.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    event_ids = [event.id for event, _summary, _tmdb_id, _title in rows]
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
    for event, summary, tmdb_id, title in rows:
        items.append(
            FeedItem(
                film_ref=film_ref(tmdb_id, title),
                film_title=title,
                event_type=event.event_type,
                confidence=event.confidence,
                occurred_at=event.occurred_at,
                created_at=event.created_at,
                summary=summary,
                provenance=event.provenance,
                sources=[
                    SourceOut(
                        url=source_url(story),
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
    #
    # The day is `created_at`, NOT `occurred_at`, on purpose: the feed is a publication log
    # (ADR-0016). A backfill or a new catalog tranche therefore lands as one tall day — that is
    # the designed behaviour, not a bug to fix by regrouping on `occurred_at`.
    day = cast(func.timezone("UTC", Event.created_at), Date)
    visible = (Film.slug.is_not(None), visible_events(), _region_visible())

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
                Film.id.label("film_id"),
                Film.tmdb_id.label("tmdb_id"),
                Film.title.label("title"),
                Film.release_date.label("release_date"),
                Film.poster_path.label("poster_path"),
                Film.status.label("status"),
                day.label("day"),
                func.count().label("event_count"),
                func.array_agg(distinct(Event.event_type)).label("event_types"),
                func.bool_or(_has_story()).label("news_backed"),
            )
            .select_from(Event)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(*visible, day.in_(select(window.c.day)))
            .group_by(Film.id, Film.tmdb_id, Film.title, Film.release_date, Film.poster_path, day)
            .order_by(day.desc(), _natural_title_col().asc(), Film.slug.asc())
        )
    ).all()

    if not rows:
        return FeedDayResponse(items=[], total=total_days or 0, limit=limit, offset=offset)

    # Fetch full events (with summaries and sources) for each (film_id, day) group.
    event_rows = (
        await session.execute(
            select(
                Event.id,
                Event.film_id,
                Event.event_type,
                Event.confidence,
                Event.provenance,
                Event.created_at,
                cast(func.timezone("UTC", Event.created_at), Date).label("event_day"),
                EventSummary.summary,
                EventSummary.edited_at,
                _has_story().label("has_story"),
            )
            .join(EventSummary, EventSummary.event_id == Event.id)
            .where(
                Event.film_id.in_({r.film_id for r in rows}),
                cast(func.timezone("UTC", Event.created_at), Date).in_({r.day for r in rows}),
                visible_events(),
            )
            .order_by(Event.occurred_at.asc(), Event.created_at.asc(), Event.id.asc())
        )
    ).all()

    event_ids = [e.id for e in event_rows]
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

    # Build per-film-day event lookups split by category so that a film-day with events
    # from both categories appears in both sections (NEU-1199).
    news_events_by_film_day: dict[tuple[UUID, date], list[EventOut]] = {}
    catalog_events_by_film_day: dict[tuple[UUID, date], list[EventOut]] = {}
    for e in event_rows:
        key = (e.film_id, e.event_day)
        target = news_events_by_film_day if e.has_story else catalog_events_by_film_day
        target.setdefault(key, []).append(
            EventOut(
                event_id=e.id,
                event_type=e.event_type,
                confidence=e.confidence,
                created_at=e.created_at,
                summary=e.summary,
                summary_edited=e.edited_at is not None,
                provenance=e.provenance,
                sources=[
                    SourceOut(
                        url=source_url(story),
                        source=outlet_label(story),
                        title=story.title,
                        published_at=story.published_at,
                    )
                    for story in cap_sources(sources_by_event.get(e.id, []))
                ],
            )
        )

    def _make_item(row: Any, events: list[EventOut], news_backed: bool) -> FeedDayItem:
        return FeedDayItem(
            film_ref=film_ref(row.tmdb_id, row.title),
            film_title=row.title,
            release_year=_release_year(row.release_date),
            poster_path=row.poster_path,
            arc_stage=derive_arc_stage(row.status),
            day=row.day,
            top_event_type=most_significant_event_type([e.event_type for e in events]),
            event_types=ordered_event_types([e.event_type for e in events]),
            event_count=len(events),
            news_backed=news_backed,
            events=events,
        )

    items: list[FeedDayItem] = []
    for row in rows:
        news_events = news_events_by_film_day.get((row.film_id, row.day), [])
        catalog_events = catalog_events_by_film_day.get((row.film_id, row.day), [])

        if news_events:
            items.append(_make_item(row, news_events, True))
        if catalog_events:
            items.append(_make_item(row, catalog_events, False))
    return FeedDayResponse(items=items, total=total_days or 0, limit=limit, offset=offset)


async def _calendar_directors(session: AsyncSession, film_ids: set[UUID]) -> dict[UUID, str]:
    """Director name(s) per film, ordered by billing, joined with ', '. Films with none omitted."""
    if not film_ids:
        return {}
    rows = (
        await session.execute(
            select(FilmCredit.film_id, Person.name)
            .join(Person, Person.id == FilmCredit.person_id)
            .where(
                FilmCredit.film_id.in_(film_ids),
                FilmCredit.credit_type == "crew",
                FilmCredit.job == "Director",
            )
            .order_by(
                FilmCredit.film_id,
                nulls_last(FilmCredit.credit_order.asc()),
                Person.name.asc(),
            )
        )
    ).all()
    names_by_film: dict[UUID, list[str]] = {}
    for film_id, name in rows:
        names_by_film.setdefault(film_id, []).append(name)
    return {film_id: ", ".join(names) for film_id, names in names_by_film.items()}


async def _calendar_stars(session: AsyncSession, film_ids: set[UUID]) -> dict[UUID, list[str]]:
    """First 3 billed cast names per film (credit_order asc, nulls last, then name)."""
    if not film_ids:
        return {}
    rn = (
        func.row_number()
        .over(
            partition_by=FilmCredit.film_id,
            order_by=(nulls_last(FilmCredit.credit_order.asc()), Person.name.asc()),
        )
        .label("rn")
    )
    ranked = (
        select(FilmCredit.film_id.label("film_id"), Person.name.label("name"), rn)
        .join(Person, Person.id == FilmCredit.person_id)
        .where(FilmCredit.film_id.in_(film_ids), FilmCredit.credit_type == "cast")
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked.c.film_id, ranked.c.name)
            .where(ranked.c.rn <= 3)
            .order_by(ranked.c.film_id, ranked.c.rn)
        )
    ).all()
    stars_by_film: dict[UUID, list[str]] = {}
    for film_id, name in rows:
        stars_by_film.setdefault(film_id, []).append(name)
    return stars_by_film


async def _calendar_genres(session: AsyncSession, film_ids: set[UUID]) -> dict[UUID, list[str]]:
    """Up to 3 genre names per film, ordered by name."""
    if not film_ids:
        return {}
    rows = (
        await session.execute(
            select(FilmGenre.film_id, Genre.name)
            .join(Genre, Genre.id == FilmGenre.genre_id)
            .where(FilmGenre.film_id.in_(film_ids))
            .order_by(FilmGenre.film_id, Genre.name.asc(), Genre.id.asc())
        )
    ).all()
    genres_by_film: dict[UUID, list[str]] = {}
    for film_id, name in rows:
        bucket = genres_by_film.setdefault(film_id, [])
        if len(bucket) < 3:
            bucket.append(name)
    return genres_by_film


async def get_calendar(session: AsyncSession, *, limit: int, offset: int) -> CalendarResponse:
    today = datetime.now(tz=UTC).date()  # Python-side, NOT SQL CURRENT_DATE
    surfaced_types = tuple(RELEASE_TYPE_BUCKETS)  # (2, 3) — derived, never drifts

    # Governing release date per (film, category): collapse to the earliest date for the
    # subject before applying the upcoming filter (NEU-1206).
    governing = (
        select(
            FilmReleaseDate.film_id.label("film_id"),
            FilmReleaseDate.release_type.label("release_type"),
            func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)).label(
                "governing_date"
            ),
        )
        .where(
            FilmReleaseDate.iso_3166_1 == CALENDAR_REGION,
            FilmReleaseDate.release_type.in_(surfaced_types),
        )
        .group_by(FilmReleaseDate.film_id, FilmReleaseDate.release_type)
        .cte("governing")
    )

    # Pagination is by DATE: limit/offset count distinct release dates (soonest first), not
    # film rows — so the UI shows "N dates at a time" with a deterministic "view more".
    # `total` is the number of distinct upcoming dates.
    visible = (
        governing.c.governing_date >= today,
        Film.slug.is_not(None),
        func.coalesce(Film.adult, False).is_(False),
        or_(Film.runtime.is_(None), Film.runtime == 0, Film.runtime >= 75),
        Film.popularity > 1.5,
    )

    distinct_dates = (
        select(governing.c.governing_date.label("d"))
        .select_from(governing)
        .join(Film, Film.id == governing.c.film_id)
        .where(*visible)
        .group_by(governing.c.governing_date)
    )
    total = await session.scalar(select(func.count()).select_from(distinct_dates.subquery()))

    window = (
        distinct_dates.order_by(governing.c.governing_date.asc())
        .limit(limit)
        .offset(offset)
        .subquery()
    )

    rows = (
        await session.execute(
            select(
                Film.id.label("film_id"),
                Film.tmdb_id.label("tmdb_id"),
                Film.title.label("title"),
                Film.release_date.label("film_release_date"),
                Film.poster_path.label("poster_path"),
                governing.c.governing_date.label("release_date"),
                governing.c.release_type.label("release_type"),
            )
            .select_from(governing)
            .join(Film, Film.id == governing.c.film_id)
            .where(*visible, governing.c.governing_date.in_(select(window.c.d)))
            # Within a date, wide (3) before limited (2) → release_type DESC.
            .order_by(
                governing.c.governing_date.asc(),
                governing.c.release_type.desc(),
                nulls_last(Film.popularity.desc()),
                Film.slug.asc(),
            )
        )
    ).all()

    film_ids = {row.film_id for row in rows}
    directors_by_film = await _calendar_directors(session, film_ids)
    stars_by_film = await _calendar_stars(session, film_ids)
    genres_by_film = await _calendar_genres(session, film_ids)

    items = [
        CalendarItem(
            film_ref=film_ref(row.tmdb_id, row.title),
            film_title=row.title,
            release_year=_release_year(row.film_release_date),
            poster_path=row.poster_path,
            release_date=row.release_date,
            release_type=RELEASE_TYPE_BUCKETS[row.release_type],
            director=directors_by_film.get(row.film_id),
            stars=stars_by_film.get(row.film_id, []),
            genres=genres_by_film.get(row.film_id, []),
        )
        for row in rows
    ]
    return CalendarResponse(items=items, total=total or 0, limit=limit, offset=offset)
