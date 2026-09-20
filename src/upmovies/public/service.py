import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CTE,
    ColumnElement,
    Date,
    Select,
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

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.follow_queries import (
    events_naming_followed_people,
    followed_film_ids,
    watchlist_film_ids,
)
from upmovies.app.models import User, UserSettings
from upmovies.catalog.models import (
    MONETIZATION_TYPES,
    Collection,
    Film,
    FilmAlternativeTitle,
    FilmAvailabilityCurrent,
    FilmCredit,
    FilmGenre,
    FilmProductionCompany,
    FilmProductionCountry,
    FilmReleaseDate,
    FilmReleaseDateChange,
    Genre,
    Person,
    ProductionCompany,
    ProductionCountry,
    WatchProvider,
)
from upmovies.catalog.ref import film_ref, parse_film_ref
from upmovies.catalog.release_grade import (
    PRIMARY_REGION,
    RELEASE_TYPE_BUCKETS,
    displayable_regions,
    is_displayable_release,
)
from upmovies.config import get_settings
from upmovies.news.catalog_events import video_key_of
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.news.visibility import region_visible, visible_events
from upmovies.public.arc import (
    derive_arc_stage,
    event_stage_rank,
    most_significant_event_type,
    ordered_event_types,
)
from upmovies.public.country import country_display_name
from upmovies.public.dto import (
    CalendarItem,
    CalendarResponse,
    CastMemberOut,
    CollectionOut,
    CollectionSearchItem,
    CollectionSearchResponse,
    CompanyOut,
    CompanySearchItem,
    CompanySearchResponse,
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
    PersonSearchItem,
    PersonSearchResponse,
    PopularPeopleResponse,
    ProviderOut,
    ReleaseDateOut,
    SourceOut,
    WhereToWatchOut,
)
from upmovies.public.ical import CalendarFeedEvent
from upmovies.public.release import release_label_for_tmdb_type
from upmovies.public.sources import cap_sources, outlet_label, source_url

MIN_QUERY_LEN = 2

CALENDAR_REGION = "US"  # single governing region for v1

ICAL_PAST_WINDOW_DAYS = 365
"""How far back `get_ical_feed` publishes. Not a setting: it is a property of what a calendar is
for, not an operational knob, and a deploy that shortened it would silently delete events from
every subscriber's calendar."""

# Significance order for two calendar rows sharing a date: the theatrical arc first (a wide
# opening is the bigger beat than a limited one), then the home release in the order it
# happens. Ordering only — which types are *on* the calendar is `RELEASE_TYPE_BUCKETS`.
_CALENDAR_BUCKET_ORDER: tuple[str, ...] = ("wide", "limited", "digital", "physical")

# A bucket nobody ranked sorts last rather than raising at import: a new displayable type is a
# cosmetic ordering question, not a reason for the container to refuse to boot.
_CALENDAR_TYPE_RANK: dict[int, int] = {
    release_type: (
        _CALENDAR_BUCKET_ORDER.index(bucket)
        if bucket in _CALENDAR_BUCKET_ORDER
        else len(_CALENDAR_BUCKET_ORDER)
    )
    for release_type, bucket in RELEASE_TYPE_BUCKETS.items()
}

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
    nq = _searchable_query(q)
    if nq is None:
        return FilmIndexResponse(items=[], total=0, limit=limit, offset=offset)
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


def _searchable_query(q: str) -> str | None:
    """The folded query, or None when it is too short to search on.

    Gates on alphanumeric count, not raw length: at least MIN_QUERY_LEN alphanumeric
    characters. One check short-circuits blank, single-character and all-punctuation
    queries ("", "a", "%", "--") to an empty page instead of an unbounded %term% scan. It
    also closes the wildcard path: "%" and "_" carry no alphanumerics, and what survives the
    fold is alphanumeric only, so no LIKE metacharacter ever reaches the database.
    """
    term = q.strip()
    if sum(1 for c in term if c.isalnum()) < MIN_QUERY_LEN:
        return None
    return _normalize_query(term)


def _name_match(nq: str, *cols: Any) -> ColumnElement[bool]:
    """Fold each column the way `_normalize_query` folded the query and substring-match."""
    pattern = f"%{nq}%"
    return or_(*(_normalized_col(col).like(pattern) for col in cols))


# A person TMDB has since deleted (`tmdb_missing_at` set) is not a follow target: nothing
# will ever be ingested against them again, so a follow would be a dead row from day one.
_LIVE_PERSON = Person.tmdb_missing_at.is_(None)


def _person_search_items(people: list[Person]) -> list[PersonSearchItem]:
    return [
        PersonSearchItem(
            id=p.id,
            name=p.name,
            known_for_department=p.known_for_department,
            profile_path=p.profile_path,
        )
        for p in people
    ]


async def get_person_search(
    session: AsyncSession, *, q: str, limit: int, offset: int
) -> PersonSearchResponse:
    """Search `catalog.person` by name / original_name (folded substring), most popular first.

    Popularity is TMDB's score, refreshed on every film ingest that credits the person; a
    NULL score sorts last so a stub row never outranks a scored one on a tie.
    """
    nq = _searchable_query(q)
    if nq is None:
        return PersonSearchResponse(items=[], total=0, limit=limit, offset=offset)
    where = (_LIVE_PERSON, _name_match(nq, Person.name, Person.original_name))
    total = await session.scalar(select(func.count()).select_from(Person).where(*where))
    people = (
        (
            await session.execute(
                select(Person)
                .where(*where)
                .order_by(nulls_last(Person.popularity.desc()), Person.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return PersonSearchResponse(
        items=_person_search_items(list(people)), total=total or 0, limit=limit, offset=offset
    )


async def get_popular_people(session: AsyncSession, *, limit: int) -> PopularPeopleResponse:
    """The onboarding grid (D-17): the most popular people who have a profile photo.

    A faceless tile is useless on a wall of faces, and a person with no popularity score has
    no claim to being "popular", so both are required rather than sorted to the end.
    """
    people = (
        (
            await session.execute(
                select(Person)
                .where(
                    _LIVE_PERSON,
                    Person.profile_path.is_not(None),
                    Person.popularity.is_not(None),
                )
                .order_by(Person.popularity.desc(), Person.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return PopularPeopleResponse(items=_person_search_items(list(people)), limit=limit)


async def get_company_search(
    session: AsyncSession, *, q: str, limit: int, offset: int
) -> CompanySearchResponse:
    """Search `catalog.production_company` by name (folded substring), alphabetical.

    Companies carry no popularity, so the order is the name itself: a stable, guessable
    order for a list the user scans by eye. It sorts on the same fold the match uses, so
    "Mission: Impossible" and "Mission Impossible" sit together whatever the DB collation
    makes of the punctuation.
    """
    nq = _searchable_query(q)
    if nq is None:
        return CompanySearchResponse(items=[], total=0, limit=limit, offset=offset)
    where = _name_match(nq, ProductionCompany.name)
    total = await session.scalar(select(func.count()).select_from(ProductionCompany).where(where))
    companies = (
        (
            await session.execute(
                select(ProductionCompany)
                .where(where)
                .order_by(_normalized_col(ProductionCompany.name).asc(), ProductionCompany.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    items = [
        CompanySearchItem(
            id=c.id, name=c.name, logo_path=c.logo_path, origin_country=c.origin_country
        )
        for c in companies
    ]
    return CompanySearchResponse(items=items, total=total or 0, limit=limit, offset=offset)


async def get_collection_search(
    session: AsyncSession, *, q: str, limit: int, offset: int
) -> CollectionSearchResponse:
    """Search `catalog.collection` (TMDB franchises) by name (folded substring), alphabetical."""
    nq = _searchable_query(q)
    if nq is None:
        return CollectionSearchResponse(items=[], total=0, limit=limit, offset=offset)
    where = _name_match(nq, Collection.name)
    total = await session.scalar(select(func.count()).select_from(Collection).where(where))
    collections = (
        (
            await session.execute(
                select(Collection)
                .where(where)
                .order_by(_normalized_col(Collection.name).asc(), Collection.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    items = [
        CollectionSearchItem(id=c.id, name=c.name, poster_path=c.poster_path) for c in collections
    ]
    return CollectionSearchResponse(items=items, total=total or 0, limit=limit, offset=offset)


async def _where_to_watch(
    session: AsyncSession, film_id: UUID, *, region: str = PRIMARY_REGION
) -> WhereToWatchOut | None:
    """This film's current where-to-watch box, or `None` if nobody carries it (D-29).

    Reads `film_availability_current` — the snapshot the provider poll rebuilds wholesale every
    run — rather than the `availability_first_seen` ledger beside it. The two answer different
    questions: the ledger says a film *was* on a service and never forgets, which is what
    `now_available` cards off; the box says where a reader can watch it *now*, so a film that
    has left every service must empty it rather than keep the last poll's answer.

    **Ordered by primary key, which here means the order TMDB listed the services.** The
    rebuild deletes and re-inserts a region in one statement in the order the poll observed —
    JustWatch's own ranking, which is what puts the service most readers have at the head of the
    list. Sorting by name instead would throw that ranking away for an alphabetical one nobody
    asked for. The key is a surrogate, but the delete-and-rebuild is what makes it carry
    meaning: the rows of a region are always written together, in one order, by one writer.
    """
    rows = (
        await session.execute(
            select(
                FilmAvailabilityCurrent.monetization_type,
                FilmAvailabilityCurrent.link,
                WatchProvider.id,
                WatchProvider.name,
                WatchProvider.logo_path,
            )
            .join(WatchProvider, WatchProvider.id == FilmAvailabilityCurrent.provider_id)
            .where(
                FilmAvailabilityCurrent.film_id == film_id,
                FilmAvailabilityCurrent.region == region,
            )
            .order_by(FilmAvailabilityCurrent.id.asc())
        )
    ).all()
    if not rows:
        return None
    buckets: dict[str, list[ProviderOut]] = {kind: [] for kind in MONETIZATION_TYPES}
    for row in rows:
        # A type outside `MONETIZATION_TYPES` raises `KeyError` rather than being dropped: the
        # table check-constrains the column to this same tuple, so one reaching here is a schema
        # that moved without this read model, which should be loud. A type *added* to the tuple
        # is the quieter half — it lands in `buckets` and then needs a field on
        # `WhereToWatchOut` and a line below, or the box silently omits it.
        buckets[row.monetization_type].append(
            ProviderOut(id=row.id, name=row.name, logo_path=row.logo_path)
        )
    # The poll writes one `region.link` onto every row of a region (see
    # `FilmAvailabilityCurrent`), so the rows never disagree: this reads the first of a set of
    # equals, and is `None` only when TMDB gave the region no link at all.
    link = next((row.link for row in rows if row.link is not None), None)
    # Named rather than splatted from `buckets`: the keys are the model's fields, and a `**`
    # would type-check against `attribution` too and hide a renamed bucket until runtime.
    return WhereToWatchOut(
        region=region,
        flatrate=buckets["flatrate"],
        rent=buckets["rent"],
        buy=buckets["buy"],
        link=link,
    )


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
            .where(Event.film_id == film.id, visible_events(), region_visible())
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
            occurred_at=event.occurred_at,
            summary=summary,  # type: ignore  — guaranteed non-null by visible_events() filter
            summary_edited=edited_at is not None,
            provenance=event.provenance,
            status=event.status,
            superseded_by=event.superseded_by,
            video_key=video_key_of(event.event_type, event.subject_key),
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

    # The one definition, shared with the event writer and with `news.visibility.region_visible`
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

    # Surface the theatrical arc (wide + limited) in any of those regions, plus the US home
    # release (digital + physical); premiere and TV are dropped, and so is a home-release date
    # in an origin country — `is_displayable_release` owns that asymmetry, which is why the
    # membership test is the predicate rather than the label alone (D-26).
    release_dates = [
        ReleaseDateOut(
            country=row.iso_3166_1,
            release_type=row.release_type,
            type_label=label,
            date=row.release_date,
            certification=row.certification,
        )
        for row in release_date_rows
        if is_displayable_release(
            iso_3166_1=row.iso_3166_1,
            release_type=row.release_type,
            origin_country=film.origin_country,
        )
        and (label := release_label_for_tmdb_type(row.release_type)) is not None
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

    company_rows = (
        await session.execute(
            select(ProductionCompany.id, ProductionCompany.name)
            .join(FilmProductionCompany, FilmProductionCompany.company_id == ProductionCompany.id)
            .where(FilmProductionCompany.film_id == film.id)
            .order_by(ProductionCompany.name.asc(), ProductionCompany.id.asc())
        )
    ).all()
    companies = [r.name for r in company_rows]
    companies_out = [CompanyOut(id=r.id, name=r.name) for r in company_rows]

    countries = (await _production_countries_for_films(session, {film.id})).get(film.id, [])

    collection: CollectionOut | None = None
    if film.collection_id is not None:
        col_row = (
            await session.execute(select(Collection).where(Collection.id == film.collection_id))
        ).scalar_one_or_none()
        if col_row is not None:
            collection = CollectionOut(
                id=col_row.id, name=col_row.name, poster_path=col_row.poster_path
            )

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
            select(Person.id, Person.name, FilmCredit.character, Person.profile_path)
            .join(FilmCredit, FilmCredit.person_id == Person.id)
            .where(FilmCredit.film_id == film.id, FilmCredit.credit_type == "cast")
            .order_by(nulls_last(FilmCredit.credit_order.asc()), Person.name.asc())
            .limit(12)
        )
    ).all()
    cast_out = [
        CastMemberOut(
            person_id=r.id, name=r.name, character=r.character, profile_path=r.profile_path
        )
        for r in cast_rows
    ]

    crew_rows = (
        await session.execute(
            select(
                Person.id,
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
        CrewMemberOut(person_id=r.id, name=r.name, job=r.job, department=r.department)
        for r in sorted(crew_rows, key=_crew_sort_key)
    ]

    return FilmDetailResponse(
        ref=film_ref(film.tmdb_id, film.title),
        id=film.id,
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
        production_countries=countries,
        production_companies=companies,
        companies=companies_out,
        collection=collection,
        alternative_titles=alternative_titles,
        cast=cast_out,
        crew=crew_out,
        where_to_watch=await _where_to_watch(session, film.id),
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
            .where(visible_events(), region_visible())
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
        .where(Film.slug.is_not(None), visible_events(), region_visible())
    )
    rows = (
        await session.execute(
            select(Event, EventSummary.summary, Film.tmdb_id, Film.title)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(Film.slug.is_not(None), visible_events(), region_visible())
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


def _feed_scope(
    film_filter: Select[tuple[UUID]] | None, event_filter: Select[tuple[UUID]] | None
) -> tuple[ColumnElement[bool], ...]:
    """`get_feed_grouped`'s narrowing as a WHERE-clause tuple: empty for the unfiltered feed,
    and the **OR** of the two filters when both are given (NEU-1365).

    OR and not AND: the two answer the same question of different grains — the timeline wants
    every event on a film the user follows *plus* every event naming a person they follow on a
    film they do not (D-11) — so intersecting them would return only the mentions that landed on
    an already-followed film, which is the one case neither filter was added for.

    Phrased on `Event.film_id` rather than `Film.id` so one clause serves every query that has to
    agree: the day queries join `Film`, the event fetch does not, and a film that reached the page
    on one named event would otherwise ship its whole day.
    """
    terms: list[ColumnElement[bool]] = []
    if film_filter is not None:
        terms.append(Event.film_id.in_(film_filter))
    if event_filter is not None:
        terms.append(Event.id.in_(event_filter))
    return (or_(*terms),) if terms else ()


async def get_feed_grouped(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    film_filter: Select[tuple[UUID]] | None = None,
    event_filter: Select[tuple[UUID]] | None = None,
) -> FeedDayResponse:
    """The grouped feed — and, given either filter, any narrowing of it (NEU-1351, NEU-1365).

    `film_filter` is a `SELECT film.id` and `event_filter` a `SELECT event.id`, both composed by
    the caller; the timeline passes the films its user follows and the events naming the people
    they follow (`app.follow_queries`). Together they narrow the *event* scope — see `_feed_scope`
    for why that is an OR — and that one scope applies identically to the day count, the day
    window, the film-day rows and the events fetched for them. Which is what makes a filtered page
    the same DTO with fewer rows in it, day pagination and all, rather than a second query to keep
    in step with this one.
    """
    # Pagination is by DAY: limit/offset count distinct days (newest first), not film rows —
    # so the UI shows "N days at a time" with a deterministic "view more". `total` is the
    # number of distinct days, so the client knows when no more days remain.
    #
    # The day is `created_at`, NOT `occurred_at`, on purpose: the feed is a publication log
    # (ADR-0016). A backfill or a new catalog tranche therefore lands as one tall day — that is
    # the designed behaviour, not a bug to fix by regrouping on `occurred_at`.
    day = cast(func.timezone("UTC", Event.created_at), Date)
    visible = (Film.slug.is_not(None), visible_events(), region_visible())
    scoped = _feed_scope(film_filter, event_filter)

    distinct_days = (
        select(day.label("day"))
        .select_from(Event)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .join(Film, Film.id == Event.film_id)
        .where(*visible, *scoped)
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
            .where(*visible, *scoped, day.in_(select(window.c.day)))
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
                Event.status,
                Event.superseded_by,
                Event.created_at,
                Event.occurred_at,
                Event.subject_key,
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
                # The same scope as the rows above, not just their (film, day) keys: a film that
                # reached the page on one event naming a followed person ships that event, not
                # every event it happened to have that day (NEU-1365).
                *scoped,
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
                occurred_at=e.occurred_at,
                summary=e.summary,
                summary_edited=e.edited_at is not None,
                provenance=e.provenance,
                status=e.status,
                superseded_by=e.superseded_by,
                video_key=video_key_of(e.event_type, e.subject_key),
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

    # Two batched lookups per page, not per row — the title parenthetical's country and
    # director elements (NEU-1215), in the same style as `sources_by_event` above.
    feed_film_ids = {r.film_id for r in rows}
    countries_by_film = await _production_countries_for_films(session, feed_film_ids)
    directors_by_film = await _directors_for_films(session, feed_film_ids)

    def _make_item(
        row: Any, events: list[EventOut], news_backed: bool, ship_events: bool = True
    ) -> FeedDayItem:
        return FeedDayItem(
            film_ref=film_ref(row.tmdb_id, row.title),
            film_title=row.title,
            release_year=_release_year(row.release_date),
            poster_path=row.poster_path,
            arc_stage=derive_arc_stage(row.status),
            production_countries=countries_by_film.get(row.film_id, []),
            directors=directors_by_film.get(row.film_id, []),
            day=row.day,
            top_event_type=most_significant_event_type([e.event_type for e in events]),
            event_types=ordered_event_types([e.event_type for e in events]),
            event_count=len(events),
            news_backed=news_backed,
            events=events if ship_events else [],
        )

    items: list[FeedDayItem] = []
    for row in rows:
        news_events = news_events_by_film_day.get((row.film_id, row.day), [])
        catalog_events = catalog_events_by_film_day.get((row.film_id, row.day), [])

        if news_events:
            items.append(_make_item(row, news_events, True))
        if catalog_events:
            items.append(_make_item(row, catalog_events, False, ship_events=False))

    # Within a day, the bigger beat leads (D-7): a casting burst outranks a status change,
    # and a trailer outranks both. Sorted here rather than in SQL because the ranking is
    # `_EVENT_STAGE`'s, and a CASE expression restating it is a second copy to keep in step.
    # A *stable* sort over rows SQL already returned in title order, so significance ranks
    # first and title still breaks its ties — the day axis is untouched, it is only re-keyed
    # here because every row of every windowed day is already in hand.
    items.sort(key=lambda item: (item.day, event_stage_rank(item.top_event_type)), reverse=True)
    return FeedDayResponse(items=items, total=total_days or 0, limit=limit, offset=offset)


async def get_timeline(
    session: AsyncSession, *, user_id: UUID, limit: int, offset: int
) -> FeedDayResponse:
    """The grouped feed restricted to what this user's follows reach (D-11, D-12).

    Two filters, OR-ed by `get_feed_grouped`, because D-11 has two halves: the films the user
    follows, and the events that *name* a person they follow on a film that person holds no credit
    on (NEU-1365). The second is event-grained on purpose — such a mention makes its own event
    timeline-worthy and says nothing about the rest of the film's history. Which is the one way a
    timeline row is not its feed row: a film-day reached by a mention alone carries that event and
    not the film's others, so `event_count`, `event_types` and `top_event_type` can read lower here
    than on `/feed` for the same film and day. The shape and the ordering are the feed's; the
    contents are what this user follows.

    **A muted film is absent from both halves** (D-45). The exclusion lives inside the two
    builders rather than here, so the digest section that summarises this timeline cannot
    disagree with it about what the user silenced.

    Deliberately `get_feed_grouped` with a filter rather than a query of its own: the timeline
    and the feed are the same product surface — same DTO, same `created_at` day grouping, same
    day pagination (ADR-0016) — and the client swaps one for the other on `/` as soon as `me`
    resolves. Two queries would be two things to keep in step.

    Takes a `user_id` rather than a `User` so nothing request-scoped reaches the filter, which
    the notify pass (NEU-1379) runs from `pipeline_run`. Entitlement is the route's gate (D-39),
    not this function's: an unentitled user gets 403 from `require_entitled()` and never arrives
    here, because an empty timeline would read as "nothing happened" rather than "you do not
    have access" (D-41).
    """
    settings = get_settings()
    return await get_feed_grouped(
        session,
        limit=limit,
        offset=offset,
        film_filter=followed_film_ids(
            user_id=user_id,
            today=datetime.now(UTC).date(),
            excluded_statuses=settings.tmdb_excluded_statuses,
        ),
        event_filter=events_naming_followed_people(user_id),
    )


async def _directors_for_films(session: AsyncSession, film_ids: set[UUID]) -> dict[UUID, list[str]]:
    """Director names per film, ordered by billing. Films with no director credit are omitted.

    Returns the names as a list rather than a joined string because the two callers punctuate
    differently — the calendar joins with ", " on its own line, the feed row joins with "/"
    inside the title parenthetical (NEU-1215).
    """
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
    return names_by_film


async def _production_countries_for_films(
    session: AsyncSession, film_ids: set[UUID]
) -> dict[UUID, list[str]]:
    """Display-form production countries per film, sorted by display name ascending.

    `film_production_country` is keyed (film_id, iso_3166_1) with no ordinal column, so TMDB's
    own ordering is discarded on write and row order carries no meaning. Sorting by display name
    is what makes a co-production's list stable across renders and re-ingests. Films with no
    countries are omitted; callers default to [].
    """
    if not film_ids:
        return {}
    rows = (
        await session.execute(
            select(
                FilmProductionCountry.film_id,
                FilmProductionCountry.iso_3166_1,
                ProductionCountry.name,
            )
            # Outer, not inner: a code whose `production_country` row is missing falls through
            # to the raw code rather than vanishing from a co-production's list. The FK on
            # `film_production_country.iso_3166_1` means that cannot happen today — the join is
            # defensive, and `country_display_name` owns the fallback (see test_country.py).
            .outerjoin(
                ProductionCountry,
                ProductionCountry.iso_3166_1 == FilmProductionCountry.iso_3166_1,
            )
            .where(FilmProductionCountry.film_id.in_(film_ids))
        )
    ).all()
    countries_by_film: dict[UUID, list[str]] = {}
    for film_id, iso_3166_1, name in rows:
        countries_by_film.setdefault(film_id, []).append(country_display_name(iso_3166_1, name))
    return {film_id: sorted(names) for film_id, names in countries_by_film.items()}


def _joined_directors(names: list[str] | None) -> str | None:
    """The calendar row's pre-joined director string — its `director` field is a `str | None`."""
    return ", ".join(names) if names else None


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


def _calendar_type_rank(release_type: ColumnElement[int]) -> ColumnElement[int]:
    """`_CALENDAR_TYPE_RANK` as a SQL expression to sort by, ascending."""
    return case(_CALENDAR_TYPE_RANK, value=release_type, else_=len(_CALENDAR_BUCKET_ORDER))


def _calendar_governing_cte(*, name: str, watchlist_user_id: UUID | None = None) -> CTE:
    """The governing release date per (film, category): the earliest date in the subject,
    collapsed *before* any window filter (NEU-1206).

    The types are `RELEASE_TYPE_BUCKETS`' keys — (2, 3, 4, 5), derived, never drifts — and the
    region filter is already US-only, which is exactly the cut the home-release types (4, 5) are
    displayable in, so widening the bucket map widens both calendars without a second region
    rule (D-26).

    `watchlist_user_id` narrows the set to that user's computed watchlist (M8): the films their
    follows cover, minus the ones they have muted. It belongs here rather than in a later
    predicate for the reason `get_ical_feed` puts it here: the collapse is per subject, so a
    user filter applied after it would be collapsing over rows the caller cannot see.
    """
    governing = select(
        FilmReleaseDate.film_id.label("film_id"),
        FilmReleaseDate.release_type.label("release_type"),
        func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)).label(
            "governing_date"
        ),
    )
    if watchlist_user_id is not None:
        settings = get_settings()
        governing = governing.where(
            FilmReleaseDate.film_id.in_(
                watchlist_film_ids(
                    user_id=watchlist_user_id,
                    today=datetime.now(tz=UTC).date(),
                    max_age_days=settings.provider_poll_max_age_days,
                )
            )
        )
    return (
        governing.where(
            FilmReleaseDate.iso_3166_1 == CALENDAR_REGION,
            FilmReleaseDate.release_type.in_(tuple(RELEASE_TYPE_BUCKETS)),
        )
        .group_by(FilmReleaseDate.film_id, FilmReleaseDate.release_type)
        .cte(name)
    )


async def _calendar_page(
    session: AsyncSession,
    *,
    governing: CTE,
    visible: tuple[ColumnElement[bool], ...],
    limit: int,
    offset: int,
) -> CalendarResponse:
    """One page of a calendar, from a governing CTE and the predicates that decide which of its
    rows the caller may see.

    The public calendar and the caller's own (`get_watchlist_calendar`) differ in exactly those
    two inputs — which films, and which cuts. Paging by date, within-date ordering and the
    decoration are spelled once, here, because the frontend renders both through one component
    and one set of grouping helpers: a page whose shape or ordering drifted would be a second
    component in disguise (D-1411.3).
    """
    # Pagination is by DATE: limit/offset count distinct release dates (soonest first), not
    # film rows — so the UI shows "N dates at a time" with a deterministic "view more".
    # `total` is the number of distinct upcoming dates.
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
            # Within a date, the theatrical arc leads and the home release follows:
            # wide, limited, digital, physical (`_CALENDAR_TYPE_RANK`). This used to be
            # `release_type DESC`, which said the same thing while only 2 and 3 existed but
            # would float physical (5) above wide (3) now that it does not.
            .order_by(
                governing.c.governing_date.asc(),
                _calendar_type_rank(governing.c.release_type),
                nulls_last(Film.popularity.desc()),
                Film.slug.asc(),
            )
        )
    ).all()

    film_ids = {row.film_id for row in rows}
    directors_by_film = await _directors_for_films(session, film_ids)
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
            director=_joined_directors(directors_by_film.get(row.film_id)),
            stars=stars_by_film.get(row.film_id, []),
            genres=genres_by_film.get(row.film_id, []),
        )
        for row in rows
    ]
    return CalendarResponse(items=items, total=total or 0, limit=limit, offset=offset)


async def get_calendar(session: AsyncSession, *, limit: int, offset: int) -> CalendarResponse:
    today = datetime.now(tz=UTC).date()  # Python-side, NOT SQL CURRENT_DATE
    governing = _calendar_governing_cte(name="governing")
    # The noise cuts that keep a public listing clean. They are the public route's alone: the
    # watchlist calendar next door deliberately carries none of them (D-1411.2).
    visible = (
        governing.c.governing_date >= today,
        Film.slug.is_not(None),
        func.coalesce(Film.adult, False).is_(False),
        or_(Film.runtime.is_(None), Film.runtime == 0, Film.runtime >= 75),
        Film.popularity > 1.5,
    )
    return await _calendar_page(
        session, governing=governing, visible=visible, limit=limit, offset=offset
    )


async def get_watchlist_calendar(
    session: AsyncSession, *, user_id: UUID, limit: int, offset: int
) -> CalendarResponse:
    """`GET /me/calendar`: the release calendar narrowed to this user's watchlist (D-34, D-39).

    Same response shape, same date-paging, same buckets, same governing-date rule and the same
    upcoming-only window as `get_calendar` — the frontend renders both tabs through one
    component, so the only thing allowed to differ is which films.

    That set is the `.ics` feed's, not the public listing's, and for the feed's reasons:

    - **The computed watchlist** (D-42): every film this user's follows cover at their
      coverage, minus the ones they have muted. Following a director puts their in-window films
      here — that is the point of the merged model, and exactly the film the user would
      otherwise miss — while a muted film leaves this page and the `.ics` feed together.
    - **No popularity, runtime or adult cut.** Those keep noise off a public listing. A film the
      user has on their own watchlist is not noise to them, and applying the cuts here would
      make this page disagree with the same user's subscribed calendar — the bug this endpoint
      exists to prevent (D-1411.2). Only the slug rule survives, for the reason every surface
      applies it: a row links to the film's page and there is no page to link.

    The window is `get_calendar`'s `>= today`, *not* the feed's reach into the past (D-1411.1).
    That reach exists for a client reason — a subscribed calendar drops every event a feed stops
    publishing — and a JSON page re-rendered on every visit has no such client; paging
    soonest-first over a past window would open page one on releases a year gone.
    """
    today = datetime.now(tz=UTC).date()  # Python-side, NOT SQL CURRENT_DATE
    governing = _calendar_governing_cte(name="watchlist_governing", watchlist_user_id=user_id)
    visible = (governing.c.governing_date >= today, Film.slug.is_not(None))
    return await _calendar_page(
        session, governing=governing, visible=visible, limit=limit, offset=offset
    )


async def get_ical_feed(
    session: AsyncSession, *, token: str
) -> tuple[CalendarFeedEvent, ...] | None:
    """The subscriber's calendar feed events, or None when there is no feed to serve (D-34).

    `None` covers three cases on purpose — no such token, a token that has been rotated away,
    and a token whose owner is not entitled — because the route answers all three with 404 and
    the caller is unauthenticated. Telling them apart is the whole risk D-39 names: a 403 for
    the third case would confirm to a stranger holding a guessed URL that the token is real.
    Folding the entitlement rule into the lookup, rather than checking it after, is what makes
    that indistinguishability structural instead of a `raise` somebody can reorder.

    `entitled_user_clause()` rather than `is_entitled()` over a loaded row: the gate is being
    applied to the *token's owner*, who is not the request's user — there is no request user at
    all here — so the request-time dependency cannot reach this, and the SQL predicate is the
    one spelling of the rule that does not need a `User` in hand (D-39).

    What the feed holds, and why it is not the public calendar's query with a user filter bolted
    on:

    - **The computed watchlist** (D-42): every film this user's follows cover at their
      coverage, minus the ones they have muted. The watchlist is the surface that is allowed to
      reach out to the user, and a calendar the user subscribed to is that — a film reached
      through a followed director is exactly the one they would otherwise miss, and a muted one
      leaves this feed and `/me/calendar` together.
    - **No popularity, runtime or adult cut.** Those keep noise off a public listing. A film the
      user has on their own watchlist is not noise to them — the same reasoning
      `digest_sender.load_slate` records for the slate.
    - **No upcoming-only filter**, unlike `get_calendar` — but a bounded reach backwards. A
      subscribed feed is the client's whole view of this calendar: a client re-fetching it drops
      every event the feed stopped publishing, so filtering to future dates would quietly erase
      each release from the user's calendar the day after it happened. Everything future is
      therefore published in full, and the past is cut at `ICAL_PAST_WINDOW_DAYS`. The cut is
      what keeps the document bounded by something other than the watchlist: an imported
      watchlist runs to thousands of films (D-15, D-16), each with up to four buckets, and this
      is a document re-fetched on the client's schedule rather than a paginated read. A release
      a year gone is not a date anyone scrolls back to; a release last month is exactly the one
      the previous paragraph exists to protect.
    - **A film with no slug is skipped**, for the reason the decision pass skips one: the event's
      DESCRIPTION is a link to the film's page, and there is no page to link.
    """
    user_id = await session.scalar(
        select(UserSettings.user_id)
        .join(User, User.id == UserSettings.user_id)
        .where(UserSettings.ical_token == token, entitled_user_clause())
    )
    if user_id is None:
        return None

    # Python-side, not SQL `CURRENT_DATE` — `get_calendar` next door takes the same care, and
    # for the same reason: the cutoff must be the same instant for every row of one response.
    today = datetime.now(tz=UTC).date()
    earliest = today - timedelta(days=ICAL_PAST_WINDOW_DAYS)
    settings = get_settings()

    # The governing date per (film, bucket): the earliest row in the subject, collapsed exactly
    # as `get_calendar` collapses it (NEU-1206), over the same displayable types in the same
    # single region — the one the home-release buckets are displayable in at all (D-26).
    governing = (
        select(
            FilmReleaseDate.film_id.label("film_id"),
            FilmReleaseDate.release_type.label("release_type"),
            func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)).label(
                "governing_date"
            ),
        )
        .where(
            FilmReleaseDate.film_id.in_(
                watchlist_film_ids(
                    user_id=user_id,
                    today=today,
                    max_age_days=settings.provider_poll_max_age_days,
                )
            ),
            FilmReleaseDate.iso_3166_1 == PRIMARY_REGION,
            FilmReleaseDate.release_type.in_(tuple(RELEASE_TYPE_BUCKETS)),
        )
        .group_by(FilmReleaseDate.film_id, FilmReleaseDate.release_type)
        .cte("ical_governing")
    )

    # DTSTAMP's source. `catalog.film_release_date` is delete-and-rebuilt on every ingest and
    # carries no timestamp of its own, so "when did this date last move?" is answered by the
    # table that exists to remember exactly that (`FilmReleaseDateChange`, NEU-1121) — per
    # subject, which is the grain the UID is keyed on.
    #
    # Correlated per row rather than a grouped subquery joined in: the change table is
    # append-only and unbounded, so grouping it whole would make every calendar fetch pay for
    # the history of the entire catalog to read a handful of watchlisted films out of it. This
    # way each row is one index seek on `ix_catalog_film_release_date_change_lookup`, and the
    # work is bounded by the size of the user's watchlist.
    last_moved = (
        select(func.max(FilmReleaseDateChange.changed_at))
        .where(
            FilmReleaseDateChange.film_id == governing.c.film_id,
            FilmReleaseDateChange.release_type == governing.c.release_type,
            FilmReleaseDateChange.iso_3166_1 == PRIMARY_REGION,
        )
        .correlate(governing)
        .scalar_subquery()
    )

    rows = (
        await session.execute(
            select(
                governing.c.film_id,
                governing.c.release_type,
                governing.c.governing_date,
                Film.tmdb_id,
                Film.title,
                # A date that has never been recorded as moving falls back to when the slate was
                # first observed, and then to the film's own row — both fixed points. Never
                # `now()`: a DTSTAMP computed per request would tell the client every event
                # changed on every poll, which is the one thing a stable UID is for.
                func.coalesce(
                    last_moved,
                    Film.release_dates_observed_at,
                    Film.created_at,
                ).label("updated_at"),
            )
            .select_from(governing)
            .join(Film, Film.id == governing.c.film_id)
            .where(Film.slug.is_not(None), governing.c.governing_date >= earliest)
            .order_by(
                governing.c.governing_date.asc(),
                _calendar_type_rank(governing.c.release_type),
                Film.title.asc(),
            )
        )
    ).all()

    return tuple(
        CalendarFeedEvent(
            film_id=str(row.film_id),
            bucket=RELEASE_TYPE_BUCKETS[row.release_type],
            title=row.title,
            release_date=row.governing_date,
            film_ref=film_ref(row.tmdb_id, row.title),
            updated_at=row.updated_at,
        )
        for row in rows
    )
