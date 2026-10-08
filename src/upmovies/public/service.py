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
    exists,
    func,
    nulls_last,
    or_,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import headline_release_out
from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.entity_names import ENTITY_TYPES, entity_names
from upmovies.app.follow_queries import (
    entity_attribution_pairs,
    entity_event_ids,
    follow_reach,
    title_follow_film_ids,
)
from upmovies.app.models import User, UserSettings
from upmovies.catalog.fold import DIACRITIC_FROM, DIACRITIC_TO
from upmovies.catalog.headline_release import HeadlineRelease, headline_releases
from upmovies.catalog.models import (
    Collection,
    Film,
    FilmAlternativeTitle,
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
)
from upmovies.catalog.queries import alert_window_clause, in_play_clause
from upmovies.catalog.ref import (
    collection_ref,
    company_ref,
    film_ref,
    parse_collection_ref,
    parse_company_ref,
    parse_film_ref,
    parse_person_ref,
    person_ref,
)
from upmovies.catalog.release_grade import (
    HOME_RELEASE_TYPES,
    PRIMARY_REGION,
    RELEASE_TYPE_BUCKETS,
    THEATRICAL_RELEASE_TYPES,
    displayable_regions,
    is_displayable_release,
)
from upmovies.catalog.seed_grade import DIRECTOR_JOB, is_seed_grade
from upmovies.config import get_settings
from upmovies.news.catalog_events import video_key_of
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.news.visibility import feed_visible, region_visible, visible_events
from upmovies.pagination import decode_cursor, encode_cursor
from upmovies.public.arc import (
    derive_arc_stage,
    event_stage_rank,
    most_significant_event_type,
    ordered_event_types,
)
from upmovies.public.country import country_display_name
from upmovies.public.dto import (
    CalendarItem,
    CalendarKind,
    CalendarResponse,
    CastMemberOut,
    CollectionDetailResponse,
    CollectionOut,
    CollectionSearchItem,
    CollectionSearchResponse,
    CompanyDetailResponse,
    CompanyOut,
    CompanySearchItem,
    CompanySearchResponse,
    CrewMemberOut,
    DayGroup,
    EntityEventsResponse,
    EventOut,
    FeedDayItem,
    FeedDayResponse,
    FeedItem,
    FeedResponse,
    FeedVia,
    FilmDetailResponse,
    FilmIndexItem,
    FilmIndexResponse,
    FilmRowOut,
    PersonCreditOut,
    PersonDetailResponse,
    PersonFilmOut,
    PersonSearchItem,
    PersonSearchResponse,
    PopularPeopleResponse,
    ReleaseDateOut,
    SourceOut,
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
_CALENDAR_BUCKET_ORDER: tuple[str, ...] = ("wide", "limited", "digital")

# The release types each calendar kind holds (D-1542.2): the theatrical arc, or the US home
# release. Derived from `release_grade`, never literal ints, so the kinds cannot drift from
# what is displayable.
CALENDAR_KIND_TYPES: dict[CalendarKind, frozenset[int]] = {
    "theatrical": THEATRICAL_RELEASE_TYPES,
    "home": HOME_RELEASE_TYPES,
}

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


_PY_DIACRITIC = str.maketrans(DIACRITIC_FROM, DIACRITIC_TO)


def _normalize_query(q: str) -> str:
    """Python-side counterpart of the stored **search fold** (`catalog.fold.fold_sql`), applied
    to the user's query. Mirrors the SQL fold exactly (same diacritic map + keep-alphanumerics)
    so the query and the `<col>_fold` columns agree across scripts."""
    folded = q.lower().translate(_PY_DIACRITIC)
    return "".join(c for c in folded if c.isalnum())


def _primary_title_match(nq: str) -> ColumnElement[bool]:
    """Match the normalized query against the film's primary title or original_title."""
    pattern = f"%{nq}%"
    return or_(
        Film.title_fold.like(pattern),
        Film.original_title_fold.like(pattern),
    )


def _title_match(nq: str) -> ColumnElement[bool]:
    """Boolean clause matching the normalized query against title/original_title/alt-titles.

    Every column is matched through its stored **search fold** (lowercase + de-accent + strip
    non-alphanumerics, ADR-0020), which the query shares, so 'spiderman' / 'spider man' find
    'Spider-Man'; each fold carries a pg_trgm GIN index, so a three-character query is an index
    probe rather than a scan. The match is one uncorrelated IN over the union of matching ids:
    each film still appears at most once, and every fold's index is probed once. An OR of the
    primary match with an alt-title subquery, correlated or not, leaves `film` on a seq scan.
    """
    matching_ids = union_all(
        select(Film.id).where(_primary_title_match(nq)),
        select(FilmAlternativeTitle.film_id).where(FilmAlternativeTitle.title_fold.like(f"%{nq}%")),
    )
    return Film.id.in_(matching_ids)


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


def _name_match(nq: str, *fold_cols: Any) -> ColumnElement[bool]:
    """Substring-match the folded query against each stored `<col>_fold` column."""
    pattern = f"%{nq}%"
    return or_(*(col.like(pattern) for col in fold_cols))


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
    where = (_LIVE_PERSON, _name_match(nq, Person.name_fold, Person.original_name_fold))
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


def _film_row_out(film: Film, headline: HeadlineRelease | None) -> FilmRowOut:
    """One film cited on an entity page (person, studio or franchise). One function because it
    is one row — a studio page and a person page disagreeing about a film's date would be the
    exact bug `FilmRowOut` exists to prevent."""
    return FilmRowOut(
        ref=film_ref(film.tmdb_id, film.title),
        id=film.id,
        tmdb_id=film.tmdb_id,
        slug=film.slug,
        title=film.title,
        poster_path=film.poster_path,
        headline_release=headline_release_out(headline),
    )


def _film_row_order_key(row: FilmRowOut, *, sign: int) -> tuple[bool, float, str]:
    """The order every entity page's two lists run in.

    Sorted in Python rather than in SQL: the headline release is two statements of its own
    (`catalog.headline_release`), so the dates are only in hand once the films are. Undated
    films sort last in both lists — the `is None` term leads the key — because a film with no
    displayable date is the least certain thing on the page whichever direction the dates run.
    `sign` flips the date alone (`-1` for `recent`, newest first), so the title tiebreak stays
    alphabetical in both.
    """
    return (
        row.headline_release is None,
        sign * (row.headline_release.date.toordinal() if row.headline_release else 0),
        row.title,
    )


def _person_film_out(
    film: Film, credits: list[PersonCreditOut], headline: HeadlineRelease | None
) -> PersonFilmOut:
    """One row of a person page. Every credit on it is one a follow delivers (EF-2), so the row
    has nothing left to qualify itself with."""
    return PersonFilmOut(film=_film_row_out(film, headline), credits=credits)


_UNBILLED = 1_000_000
"""Where a credit with no billing position sorts: after every billed one.

A sentinel rather than `None` because the sort key is a tuple and `None` does not compare
against an `int`. Crew rows and the long tail of a cast list both arrive without an `order`."""


async def get_person_detail(session: AsyncSession, ref: str) -> PersonDetailResponse | None:
    """A person's page (D-1416.6), or None for an unknown or tombstoned person.

    **Two lists, and between them exactly what a follow can reach.** `upcoming` is the in-play
    set (`in_play_clause`, the D-11 timeline's bound) and `recent` is the alert window
    (`alert_window_clause`, D-46) less the in-play set, so every film is in one or the other
    and never both. Nothing older is returned at all: the page's job is to show what following
    this person would deliver, and a filmography stretching back thirty years answers a
    different question — one `/films/search` already answers.

    **Tombstoned people 404 rather than rendering empty.** `tmdb_missing_at` means TMDB has
    deleted the person; they are not a follow target anywhere else (`_LIVE_PERSON`, used by
    search and the onboarding grid), so a page offering a follow button for one would offer a
    row that is dead from the moment it is written.

    **Every credit is listed, and every one of them is reached by a follow** (EF-2). The tier
    badge D-48 hung on each row is gone with the tier itself: the page's answer to "what would
    following them deliver?" is now the list, unqualified.
    """
    person_id = parse_person_ref(ref)
    if person_id is None:
        return None
    person = (
        await session.execute(select(Person).where(Person.id == person_id, _LIVE_PERSON))
    ).scalar_one_or_none()
    if person is None:
        return None

    settings = get_settings()
    today = datetime.now(UTC).date()
    in_play = in_play_clause(today=today, excluded_statuses=settings.tmdb_excluded_statuses)
    window = alert_window_clause(today=today, max_age_days=settings.provider_poll_max_age_days)
    rows = (
        await session.execute(
            select(
                Film,
                FilmCredit.credit_type,
                FilmCredit.job,
                FilmCredit.character,
                FilmCredit.credit_order,
                in_play.label("in_play"),
            )
            .join(FilmCredit, FilmCredit.film_id == Film.id)
            .where(FilmCredit.person_id == person.id, or_(in_play, window))
        )
    ).all()

    films: dict[UUID, Film] = {}
    credits: dict[UUID, list[PersonCreditOut]] = {}
    upcoming_ids: set[UUID] = set()
    for row in rows:
        film = row[0]
        films[film.id] = film
        credits.setdefault(film.id, []).append(
            PersonCreditOut(
                credit_type=row.credit_type,
                job=row.job,
                character=row.character,
                credit_order=row.credit_order,
            )
        )
        if row.in_play:
            upcoming_ids.add(film.id)
    headlines = await headline_releases(session, list(films), today=today)

    def rows_for(film_ids: set[UUID], *, descending: bool) -> list[PersonFilmOut]:
        items = [
            _person_film_out(films[fid], _ordered_credits(credits[fid]), headlines.get(fid))
            for fid in film_ids
        ]
        sign = -1 if descending else 1
        return sorted(items, key=lambda i: _film_row_order_key(i.film, sign=sign))

    return PersonDetailResponse(
        ref=person_ref(person.id, person.name),
        id=person.id,
        name=person.name,
        profile_path=person.profile_path,
        known_for_department=person.known_for_department,
        birthday=person.birthday,
        deathday=person.deathday,
        upcoming=rows_for(upcoming_ids, descending=False),
        recent=rows_for(set(films) - upcoming_ids, descending=True),
    )


def _ordered_credits(credits: list[PersonCreditOut]) -> list[PersonCreditOut]:
    """A film's credits, director first, then the rest of seed grade, then everything else —
    and within each, billing before job. So "Director · Writer" reads in that order and two
    renderings of the same row cannot differ.

    **A deliberate rule, not the tier rank rewritten.** The old key sorted on `credit_tier`,
    and it did not say this: `lead` folded the director in with the top-3 billed, so a
    director who was also 2nd-billed rendered "<character> · Director" while one who was
    4th-billed rendered "Director · <character>". That was an accident of a cut built to
    decide what a follow delivered, and EF-1 deleted the cut. Ranking the director outright is
    what the page was always trying to say — it is the credit a film is attributed to — so the
    inconsistency goes with the tier rather than being preserved.

    This is presentation only. It borrows `seed_grade`'s primitives because they already name
    "the roles a film is known by"; it decides nothing about what a follow delivers, which
    since EF-2 is every credit here whatever its rank.
    """
    return sorted(
        credits,
        key=lambda c: (
            _credit_rank(c),
            c.credit_order if c.credit_order is not None else _UNBILLED,
            c.job or "",
        ),
    )


def _credit_rank(credit: PersonCreditOut) -> int:
    """`0` for a director credit, `1` for any other seed-grade role, `2` for the rest."""
    if credit.credit_type == "crew" and credit.job == DIRECTOR_JOB:
        return 0
    return 1 if is_seed_grade(credit.credit_type, credit.job, credit.credit_order) else 2


async def _entity_film_lists(
    session: AsyncSession, films_of_entity: Select[tuple[Film]]
) -> tuple[list[FilmRowOut], list[FilmRowOut]]:
    """The `(upcoming, recent)` pair every entity page returns, given a statement selecting that
    entity's films.

    **Two lists, and between them exactly what a follow can reach.** `upcoming` is the in-play
    set (`in_play_clause`, the D-11 timeline's bound) and `recent` is the alert window
    (`alert_window_clause`, D-46) less the in-play set, so every film is in one or the other and
    never both. Nothing older is returned at all: the page's job is to show what following this
    entity would deliver, and a back catalogue stretching back thirty years answers a different
    question — one `/films/search` already answers.

    The caller passes only the membership half of the query (which company, which collection),
    because that is the only thing the studio and franchise pages disagree about. The window
    split, the headline dates and the ordering are this project's rule, spelled once.
    """
    settings = get_settings()
    today = datetime.now(UTC).date()
    in_play = in_play_clause(today=today, excluded_statuses=settings.tmdb_excluded_statuses)
    window = alert_window_clause(today=today, max_age_days=settings.provider_poll_max_age_days)
    rows = (
        await session.execute(
            films_of_entity.add_columns(in_play.label("in_play")).where(or_(in_play, window))
        )
    ).all()

    films: dict[UUID, Film] = {row[0].id: row[0] for row in rows}
    upcoming_ids = {row[0].id for row in rows if row.in_play}
    headlines = await headline_releases(session, list(films), today=today)

    def rows_for(film_ids: set[UUID], *, descending: bool) -> list[FilmRowOut]:
        items = [_film_row_out(films[fid], headlines.get(fid)) for fid in film_ids]
        sign = -1 if descending else 1
        return sorted(items, key=lambda row: _film_row_order_key(row, sign=sign))

    return rows_for(upcoming_ids, descending=False), rows_for(
        set(films) - upcoming_ids, descending=True
    )


async def get_company_detail(session: AsyncSession, ref: str) -> CompanyDetailResponse | None:
    """A studio's page (EF-17), or None for an id the catalog does not hold.

    The person page's shape without its credits: a studio's relationship to a film is a single
    membership row (`catalog.film_production_company`), so there is no job to name and no grade
    to badge — the film either counts as the studio's or it does not.
    """
    company_id = parse_company_ref(ref)
    if company_id is None:
        return None
    company = (
        await session.execute(select(ProductionCompany).where(ProductionCompany.id == company_id))
    ).scalar_one_or_none()
    if company is None:
        return None

    upcoming, recent = await _entity_film_lists(
        session,
        select(Film)
        .join(FilmProductionCompany, FilmProductionCompany.film_id == Film.id)
        .where(FilmProductionCompany.company_id == company.id),
    )
    return CompanyDetailResponse(
        ref=company_ref(company.id, company.name),
        id=company.id,
        name=company.name,
        logo_path=company.logo_path,
        upcoming=upcoming,
        recent=recent,
    )


async def get_collection_detail(session: AsyncSession, ref: str) -> CollectionDetailResponse | None:
    """A franchise's page (EF-17), or None for an id the catalog does not hold.

    `get_company_detail` over `catalog.collection`. Membership is a column on the film itself
    (`film.collection_id` — TMDB gives a film at most one collection) rather than a join table,
    which is the only difference between the two.
    """
    collection_id = parse_collection_ref(ref)
    if collection_id is None:
        return None
    collection = (
        await session.execute(select(Collection).where(Collection.id == collection_id))
    ).scalar_one_or_none()
    if collection is None:
        return None

    upcoming, recent = await _entity_film_lists(
        session, select(Film).where(Film.collection_id == collection.id)
    )
    return CollectionDetailResponse(
        ref=collection_ref(collection.id, collection.name),
        id=collection.id,
        name=collection.name,
        poster_path=collection.poster_path,
        upcoming=upcoming,
        recent=recent,
    )


ENTITY_EVENTS_PAGE_SIZE = 20
"""The entity pages' card list is a page of 20 (EF-18, §6 M3).

A default rather than a fixed count: the ceiling below is the abuse bound every list route in
this router carries, and the page the product specifies is what a client that says nothing
gets."""

_ENTITY_EVENT_LOOKUPS: dict[str, tuple[Any, Any, Any]] = {
    "person": (Person, parse_person_ref, _LIVE_PERSON),
    "company": (ProductionCompany, parse_company_ref, None),
    "franchise": (Collection, parse_collection_ref, None),
}
"""How each entity page resolves the `ref` in its URL: the catalog row to prove exists, the
parser for `<id>-<slug>`, and the extra term the detail route applies.

Keyed by the **follow** graph's word, which is what `follow_queries` takes — a franchise is
`franchise` here and `catalog.Collection` beside it (CONTEXT.md **Franchise**). The existence
rules are the detail routes', deliberately: a `ref` that 404s on `/people/{ref}` and 200s on
`/people/{ref}/events` would be a page whose two halves disagree about whether it exists, and
the tombstone term is the case that actually arises (`_LIVE_PERSON` — TMDB has deleted them,
so they are not a follow target anywhere)."""


async def get_entity_events(
    session: AsyncSession,
    *,
    entity_type: str,
    ref: str,
    limit: int,
    cursor: str | None,
) -> EntityEventsResponse | None:
    """One page of an entity's own attach, detach and `canceled` cards (EF-18), or `None` for a
    `ref` the catalog does not hold.

    **What a follow of this entity would deliver**, which is why the selection is
    `follow_queries.entity_event_ids` and not a query of this module's own: the page sits
    beside the follow button and its promise is "this is what you would get". A second
    spelling here would be a promise that goes stale the first time EF-3's rule moves — and it
    has moved twice already this project.

    Public, like the rest of this router: the page renders for an anonymous visitor and the
    follow button is the thing that asks for an account.

    Newest first by `created_at`, which is the feed's axis (ADR-0016) and not the film page's
    `occurred_at`: this list answers "what has happened with them lately", so it orders by when
    we carded a beat rather than by when the beat is dated. Ties break on `id` so the keyset
    cannot drop or repeat a card, and one row beyond the page is fetched to learn whether there
    is a next one without counting a list that is still growing.
    """
    model, parse_ref, extra = _ENTITY_EVENT_LOOKUPS[entity_type]
    entity_id = parse_ref(ref)
    if entity_id is None:
        return None
    terms = [model.id == entity_id] + ([] if extra is None else [extra])
    if not await session.scalar(select(exists().where(*terms))):
        return None

    filters: list[ColumnElement[bool]] = [
        Event.id.in_(entity_event_ids(entity_type, entity_id)),
        *feed_visible(),
    ]
    if cursor is not None:
        created_at, event_id = decode_cursor(cursor)
        filters.append(tuple_(Event.created_at, Event.id) < (created_at, event_id))

    rows = (
        await session.execute(
            select(Event, EventSummary.summary, EventSummary.edited_at)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(*filters)
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(limit + 1)
        )
    ).all()

    page = rows[:limit]
    sources = await _sources_by_event(session, [event.id for event, _summary, _edited in page])
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    return EntityEventsResponse(
        items=[
            _event_out(event, summary, edited_at, sources.get(event.id, []))
            for event, summary, edited_at in page
        ],
        next_cursor=next_cursor,
    )


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
    where = _name_match(nq, ProductionCompany.name_fold)
    total = await session.scalar(select(func.count()).select_from(ProductionCompany).where(where))
    companies = (
        (
            await session.execute(
                select(ProductionCompany)
                .where(where)
                .order_by(ProductionCompany.name_fold.asc(), ProductionCompany.id.asc())
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
    where = _name_match(nq, Collection.name_fold)
    total = await session.scalar(select(func.count()).select_from(Collection).where(where))
    collections = (
        (
            await session.execute(
                select(Collection)
                .where(where)
                .order_by(Collection.name_fold.asc(), Collection.id.asc())
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


async def _sources_by_event(
    session: AsyncSession, event_ids: list[UUID]
) -> dict[UUID, list[Story]]:
    """`{event_id: [story]}` for a page of cards, oldest story first.

    One statement for the page rather than one per card, and one spelling for the three
    surfaces that render `SourceOut` lists — the flat feed, the film page and the entity pages'
    `/events` (EF-18). The order is the `sources` order the card shows: published first, then
    by id so an outlet with no date still lands somewhere stable.
    """
    if not event_ids:
        return {}
    rows = (
        await session.execute(
            select(EventStory.event_id, Story)
            .join(Story, Story.id == EventStory.story_id)
            .where(EventStory.event_id.in_(event_ids))
            .order_by(nulls_last(Story.published_at.asc()), Story.id.asc())
        )
    ).all()
    by_event: dict[UUID, list[Story]] = {}
    for event_id, story in rows:
        by_event.setdefault(event_id, []).append(story)
    return by_event


def _event_out(
    event: Any, summary: str | None, edited_at: datetime | None, sources: list[Story]
) -> EventOut:
    """One card as the API renders it, wherever it is rendered.

    `summary` is typed nullable because the column is, and is never null here: every caller
    joins `news.event_summary` inner, which is what makes the join part of "the feed's
    visibility terms" rather than a convenience.
    """
    return EventOut(
        event_id=event.id,
        event_type=event.event_type,
        confidence=event.confidence,
        created_at=event.created_at,
        occurred_at=event.occurred_at,
        summary=summary,  # type: ignore  — guaranteed non-null by the EventSummary join
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
            for story in cap_sources(sources)
        ],
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

    sources_by_event = await _sources_by_event(
        session, [event.id for event, _summary, _edited_at, _has_story in summarized]
    )

    # Group events by UTC day key, split by has_story (NEU-1201).
    day_groups: list[DayGroup] = []
    day_events: dict[date, tuple[list[EventOut], list[EventOut]]] = {}
    for event, summary, edited_at, has_story in summarized:
        utc = event.occurred_at.astimezone(UTC)
        day_key = date(utc.year, utc.month, utc.day)
        eout = _event_out(event, summary, edited_at, sources_by_event.get(event.id, []))
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
    # release (digital; physical left in NEU-1542); premiere and TV are dropped, and so is a
    # home-release date in an origin country — `is_displayable_release` owns that asymmetry,
    # which is why the membership test is the predicate rather than the label alone (D-26).
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
    )


@dataclass
class SitemapFilm:
    ref: str
    lastmod: datetime


@dataclass
class SitemapEntity:
    """One entity page in the sitemap. `path` is the frontend's route segment for that type —
    `person`, `studio` or `franchise` (EF-19's on-screen vocabulary, which the URLs follow even
    though the code says `company` and `collection`)."""

    path: str
    ref: str


async def get_sitemap_entities(session: AsyncSession) -> list[SitemapEntity]:
    """Every person, studio and franchise page worth crawling (EF-17).

    **An entity is listed when it has at least one film in reach** — in play, or inside the
    alert window — which is exactly the set its page renders. The alternative, listing every
    row in three catalog tables, would submit hundreds of thousands of pages whose whole content
    is "No upcoming films", and a crawler is entitled to read a sitemap as a claim that the URLs
    on it are worth fetching.

    **No `lastmod`, unlike the film rows.** A film page's freshness is the news on it and
    `event.created_at` states it exactly; an entity page changes when any of its films moves,
    which is not a timestamp this schema holds. `lastmod` is optional in the protocol and an
    invented one is worse than none — a wrong date teaches the crawler to ignore the field.
    """
    settings = get_settings()
    today = datetime.now(UTC).date()
    in_reach = or_(
        in_play_clause(today=today, excluded_statuses=settings.tmdb_excluded_statuses),
        alert_window_clause(today=today, max_age_days=settings.provider_poll_max_age_days),
    )

    people = (
        await session.execute(
            select(Person.id, Person.name)
            .join(FilmCredit, FilmCredit.person_id == Person.id)
            .join(Film, Film.id == FilmCredit.film_id)
            .where(_LIVE_PERSON, in_reach)
            .group_by(Person.id, Person.name)
            .order_by(Person.id.asc())
        )
    ).all()
    companies = (
        await session.execute(
            select(ProductionCompany.id, ProductionCompany.name)
            .join(
                FilmProductionCompany,
                FilmProductionCompany.company_id == ProductionCompany.id,
            )
            .join(Film, Film.id == FilmProductionCompany.film_id)
            .where(in_reach)
            .group_by(ProductionCompany.id, ProductionCompany.name)
            .order_by(ProductionCompany.id.asc())
        )
    ).all()
    collections = (
        await session.execute(
            select(Collection.id, Collection.name)
            .join(Film, Film.collection_id == Collection.id)
            .where(in_reach)
            .group_by(Collection.id, Collection.name)
            .order_by(Collection.id.asc())
        )
    ).all()

    return [
        *(SitemapEntity(path="person", ref=person_ref(id_, name)) for id_, name in people),
        *(SitemapEntity(path="studio", ref=company_ref(id_, name)) for id_, name in companies),
        *(
            SitemapEntity(path="franchise", ref=collection_ref(id_, name))
            for id_, name in collections
        ),
    ]


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
        .where(*feed_visible())
    )
    rows = (
        await session.execute(
            select(Event, EventSummary.summary, Film.tmdb_id, Film.title)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(*feed_visible())
            .order_by(Event.created_at.desc(), Event.id.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    sources_by_event = await _sources_by_event(
        session, [event.id for event, _summary, _tmdb_id, _title in rows]
    )

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


def _publication_day() -> ColumnElement[date]:
    """An event's feed day: the UTC calendar date of its `created_at` (ADR-0016).

    `created_at`, NOT `occurred_at`, on purpose: the feed is a publication log. A backfill or a
    new catalog tranche therefore lands as one tall day — that is the designed behaviour, not a
    bug to fix by regrouping on `occurred_at`."""
    return cast(func.timezone("UTC", Event.created_at), Date)


def _feed_event_columns() -> tuple[Any, ...]:
    """What `_event_out` reads off a fetched event row (the event's own columns, its summary,
    its feed day and its section), for both of the grouped feed's event fetches — the film-day
    rows' and the timeline's entity rows'."""
    return (
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
        _publication_day().label("event_day"),
        EventSummary.summary,
        EventSummary.edited_at,
        _has_story().label("has_story"),
    )


def _feed_day_item(
    film: Any,
    *,
    day: date,
    events: list[EventOut],
    news_backed: bool,
    countries_by_film: dict[UUID, list[str]],
    directors_by_film: dict[UUID, list[str]],
    via: FeedVia | None = None,
) -> FeedDayItem:
    """One grouped-feed row. `film` is any row carrying `film_id`, `tmdb_id`, `title`,
    `release_date`, `poster_path` and `film_status`; `event_count`, `event_types` and
    `top_event_type` are computed over `events` and nothing else (NEU-1199)."""
    return FeedDayItem(
        film_ref=film_ref(film.tmdb_id, film.title),
        film_title=film.title,
        release_year=_release_year(film.release_date),
        poster_path=film.poster_path,
        arc_stage=derive_arc_stage(film.film_status),
        production_countries=countries_by_film.get(film.film_id, []),
        directors=directors_by_film.get(film.film_id, []),
        day=day,
        top_event_type=most_significant_event_type([e.event_type for e in events]),
        event_types=ordered_event_types([e.event_type for e in events]),
        event_count=len(events),
        news_backed=news_backed,
        events=events,
        via=via,
    )


async def _feed_day_window(
    session: AsyncSession, scope: tuple[ColumnElement[bool], ...], *, limit: int, offset: int
) -> tuple[int, list[date]]:
    """`(total, days)`: how many feed days hold a visible event in `scope`, and this page's.

    Pagination is by DAY: limit/offset count distinct days (newest first), not rows — so the UI
    shows "N days at a time" with a deterministic "view more". `total` is the number of distinct
    days, so the client knows when no more days remain.
    """
    day = _publication_day()
    distinct_days = (
        select(day.label("day"))
        .select_from(Event)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .join(Film, Film.id == Event.film_id)
        .where(*feed_visible(), *scope)
        .group_by(day)
    )
    total = await session.scalar(select(func.count()).select_from(distinct_days.subquery()))
    days = await session.scalars(distinct_days.order_by(day.desc()).limit(limit).offset(offset))
    return total or 0, list(days)


async def _film_day_items(
    session: AsyncSession, days: list[date], scope: tuple[ColumnElement[bool], ...] = ()
) -> list[FeedDayItem]:
    """One row per (film, day, section) over `days`, each holding the visible events `scope`
    lets through — the whole grouped feed with no scope, a title follower's films with one.

    `scope` applies to the film-day rows *and* to the events fetched for them, so a scope over
    events rather than films could never ship a film's unscoped events alongside its scoped ones.
    """
    if not days:
        return []
    day = _publication_day()
    visible = feed_visible()

    rows = (
        await session.execute(
            select(
                Film.id.label("film_id"),
                Film.tmdb_id.label("tmdb_id"),
                Film.title.label("title"),
                Film.release_date.label("release_date"),
                Film.poster_path.label("poster_path"),
                Film.status.label("film_status"),
                day.label("day"),
            )
            .select_from(Event)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(*visible, *scope, day.in_(days))
            .group_by(Film.id, Film.tmdb_id, Film.title, Film.release_date, Film.poster_path, day)
            .order_by(day.desc(), _natural_title_col().asc(), Film.slug.asc())
        )
    ).all()

    if not rows:
        return []

    # Fetch full events (with summaries and sources) for each (film_id, day) group.
    event_rows = (
        await session.execute(
            select(*_feed_event_columns())
            .join(EventSummary, EventSummary.event_id == Event.id)
            .where(
                Event.film_id.in_({r.film_id for r in rows}),
                day.in_({r.day for r in rows}),
                visible_events(),
                *scope,
            )
            .order_by(Event.occurred_at.asc(), Event.created_at.asc(), Event.id.asc())
        )
    ).all()
    sources_by_event = await _sources_by_event(session, [e.id for e in event_rows])

    # Build per-film-day event lookups split by category so that a film-day with events
    # from both categories appears in both sections (NEU-1199).
    news_events_by_film_day: dict[tuple[UUID, date], list[EventOut]] = {}
    catalog_events_by_film_day: dict[tuple[UUID, date], list[EventOut]] = {}
    for e in event_rows:
        target = news_events_by_film_day if e.has_story else catalog_events_by_film_day
        target.setdefault((e.film_id, e.event_day), []).append(
            _event_out(e, e.summary, e.edited_at, sources_by_event.get(e.id, []))
        )

    # Two batched lookups per page, not per row — the title parenthetical's country and
    # director elements (NEU-1215), in the same style as `sources_by_event` above.
    feed_film_ids = {r.film_id for r in rows}
    countries_by_film = await _production_countries_for_films(session, feed_film_ids)
    directors_by_film = await _directors_for_films(session, feed_film_ids)

    items: list[FeedDayItem] = []
    for row in rows:
        for events, news_backed in (
            (news_events_by_film_day.get((row.film_id, row.day), []), True),
            (catalog_events_by_film_day.get((row.film_id, row.day), []), False),
        ):
            if events:
                items.append(
                    _feed_day_item(
                        row,
                        day=row.day,
                        events=events,
                        news_backed=news_backed,
                        countries_by_film=countries_by_film,
                        directors_by_film=directors_by_film,
                    )
                )

    # Within a day, the bigger beat leads (D-7): a casting burst outranks a status change,
    # and a trailer outranks both. Sorted here rather than in SQL because the ranking is
    # `_EVENT_STAGE`'s, and a CASE expression restating it is a second copy to keep in step.
    # A *stable* sort over rows SQL already returned in title order, so significance ranks
    # first and title still breaks its ties — the day axis is untouched, it is only re-keyed
    # here because every row of every windowed day is already in hand.
    items.sort(key=lambda item: (item.day, event_stage_rank(item.top_event_type)), reverse=True)
    return items


async def _entity_day_items(
    session: AsyncSession, *, user_id: UUID, days: list[date]
) -> list[FeedDayItem]:
    """The timeline's entity rows over `days`: one per (followed entity, film, day, section),
    each holding only the cards that reached the user through that entity (FB-13).

    Read from `entity_attribution_pairs` — the pairs the digest reads — joined to `news.event`
    under the feed's visibility terms (FB-14); the builder carries none of its own. A card two
    followed entities reached is a row under each (FB-5).

    Ordered `day DESC`, then entity type (person, company, franchise), name, film title. The
    frontend re-sorts within a day (FB-6); this order is for stable output.
    """
    if not days:
        return []
    reach = entity_attribution_pairs(user_id).subquery("reach")
    day = _publication_day()
    event_rows = (
        await session.execute(
            select(
                reach.c.entity_type,
                reach.c.entity_id,
                *_feed_event_columns(),
                Film.tmdb_id,
                Film.title,
                Film.release_date,
                Film.poster_path,
                Film.status.label("film_status"),
            )
            .select_from(reach)
            .join(Event, Event.id == reach.c.event_id)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .join(Film, Film.id == Event.film_id)
            .where(*feed_visible(), day.in_(days))
            .order_by(Event.occurred_at.asc(), Event.created_at.asc(), Event.id.asc())
        )
    ).all()
    if not event_rows:
        return []

    sources_by_event = await _sources_by_event(session, list({e.id for e in event_rows}))
    film_ids = {e.film_id for e in event_rows}
    countries_by_film = await _production_countries_for_films(session, film_ids)
    directors_by_film = await _directors_for_films(session, film_ids)
    names = await entity_names(session, {(e.entity_type, e.entity_id) for e in event_rows})

    groups: dict[tuple[str, str, UUID, date, bool], list[Any]] = {}
    for e in event_rows:
        key = (e.entity_type, e.entity_id, e.film_id, e.event_day, e.has_story)
        groups.setdefault(key, []).append(e)

    items: list[FeedDayItem] = []
    for (entity_type, entity_id, _film_id, event_day, has_story), grouped in groups.items():
        resolved = names[(entity_type, entity_id)]
        items.append(
            _feed_day_item(
                grouped[0],
                day=event_day,
                events=[
                    _event_out(e, e.summary, e.edited_at, sources_by_event.get(e.id, []))
                    for e in grouped
                ],
                news_backed=has_story,
                countries_by_film=countries_by_film,
                directors_by_film=directors_by_film,
                # Validated rather than constructed: `entity_type` is the pair builder's text,
                # and the model's `Literal` is what holds it to the three entity words.
                via=FeedVia.model_validate(
                    {
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "name": None if resolved is None else resolved.name,
                        "ref": None if resolved is None else resolved.ref,
                    }
                ),
            )
        )

    def order(item: FeedDayItem) -> tuple[Any, ...]:
        assert item.via is not None
        name = item.via.name
        return (
            -item.day.toordinal(),
            ENTITY_TYPES.index(item.via.entity_type),
            name is None,
            (name or "").casefold(),
            item.via.entity_id,
            item.film_title.casefold(),
            item.film_ref,
            not item.news_backed,
        )

    return sorted(items, key=order)


async def get_feed_grouped(session: AsyncSession, *, limit: int, offset: int) -> FeedDayResponse:
    """The grouped feed: one row per (film, publication day, section), a page of days at a time
    (ADR-0016, NEU-1199). Every row's `via` is null — the feed reaches nobody through a follow
    (FB-12)."""
    total, days = await _feed_day_window(session, (), limit=limit, offset=offset)
    items = await _film_day_items(session, days)
    return FeedDayResponse(items=items, total=total, limit=limit, offset=offset)


async def get_timeline(
    session: AsyncSession, *, user_id: UUID, limit: int, offset: int
) -> FeedDayResponse:
    """The grouped feed restricted to what this user's follows deliver (EF-3, D-12), one row per
    **reach** (FB-13, ADR-0022).

    A timeline row is a feed row with a reach: (reach, film, day, section). A follow delivers at
    two grains (ADR-0019), and each grain gives its own rows:

    - **Title rows** (`via` null) — a title follow delivers every beat on its film, so these are
      the feed's own rows for the followed films, built by the same `_film_day_items` the feed's
      are, in the feed's order.
    - **Entity rows** (`via` names the entity) — a person, studio or franchise follow delivers the
      cards in which that entity attaches to or detaches from a film, plus that film's
      cancellation (`entity_attribution_pairs`). An entity row holds only the cards that reached
      the user through *that* entity, so its `event_count`, `event_types` and `top_event_type`
      can read lower than the film's feed row for the same day.

    The two sets are not de-duplicated against each other (FB-5): a casting card on a film the
    user follows by title and whose director they follow is a line in the title row *and* in the
    director's row, and a cancellation reaching two followed entities is in both of theirs. Each
    row says how it arrived.

    **The day window is the union of both reaches** — `follow_reach`'s two halves OR-ed — so
    `total`, `limit` and `offset` count the days on which *either* reach published something, and
    a page is still "N days", exactly as on `/feed/grouped`. Both row sets are then built over
    those days; a day reached only through a followed director is a page of the timeline holding
    only entity rows. Rows run `day DESC`, title rows before entity rows within a day.

    **Nothing subtracts from either half** (EF-14). The only way off this timeline is to unfollow
    — which takes the film or the entity out of the builders themselves, so the digest that
    reproduces this timeline still cannot disagree with it about what the user asked for.

    The same DTO, the same `created_at` day grouping and the same row builder as the feed, so the
    client renders either and a title row is byte-for-byte its feed row.

    Takes a `user_id` rather than a `User` so nothing request-scoped reaches the builders.
    Entitlement is the route's gate (D-39), not this function's: an unentitled user gets 403
    from `require_entitled()` and never arrives here, because an empty timeline would read as
    "nothing happened" rather than "you do not have access" (D-41).
    """
    by_title, by_entity = follow_reach(user_id)
    total, days = await _feed_day_window(
        session, (or_(by_title, by_entity),), limit=limit, offset=offset
    )
    title_items = await _film_day_items(session, days, (by_title,))
    entity_items = await _entity_day_items(session, user_id=user_id, days=days)
    # Stable, so each day keeps its title rows (in the feed's order) ahead of its entity rows.
    items = sorted([*title_items, *entity_items], key=lambda item: item.day, reverse=True)
    return FeedDayResponse(items=items, total=total, limit=limit, offset=offset)


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


def _calendar_governing_cte(
    *,
    name: str,
    title_follow_user_id: UUID | None = None,
    release_types: frozenset[int] | None = None,
) -> CTE:
    """The governing release date per (film, category): the earliest date in the subject,
    collapsed *before* any window filter (NEU-1206).

    The types are `RELEASE_TYPE_BUCKETS`' keys — (2, 3, 4), derived, never drifts — and the
    region filter is already US-only, which is exactly the cut the home-release type (4) is
    displayable in, so widening the bucket map widens both calendars without a second region
    rule (D-26).

    `release_types` narrows that to one calendar kind (`CALENDAR_KIND_TYPES`, D-1542.2). It is
    applied here, inside the collapse, so the date paging and `total` a caller builds on this
    CTE are that kind's alone. `None` keeps every displayable type — both kinds, which is what
    an older client sends and what the `.ics` feed and the slate are (D-1542.4).

    `title_follow_user_id` narrows the set to the films that user follows **by title** (EF-14)
    — the whole of "my films" now that nothing indirect reaches a film. It belongs here rather
    than in a later predicate for the reason `get_ical_feed` puts it here: the collapse is per
    subject, so a user filter applied after it would be collapsing over rows the caller cannot
    see.
    """
    governing = select(
        FilmReleaseDate.film_id.label("film_id"),
        FilmReleaseDate.release_type.label("release_type"),
        func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)).label(
            "governing_date"
        ),
    )
    if title_follow_user_id is not None:
        governing = governing.where(
            FilmReleaseDate.film_id.in_(title_follow_film_ids(title_follow_user_id))
        )
    return (
        governing.where(
            FilmReleaseDate.iso_3166_1 == CALENDAR_REGION,
            FilmReleaseDate.release_type.in_(
                tuple(RELEASE_TYPE_BUCKETS if release_types is None else release_types)
            ),
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

    The public calendar and the caller's own (`get_my_films_calendar`) differ in exactly those
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
            # wide, limited, digital (`_CALENDAR_TYPE_RANK`). This used to be
            # `release_type DESC`, which said the same thing while only 2 and 3 existed but
            # would float digital (4) above wide (3) now that it does not.
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


def _kind_types(kind: CalendarKind | None) -> frozenset[int] | None:
    """The release types one calendar kind holds, or `None` (both kinds) when none was asked."""
    return None if kind is None else CALENDAR_KIND_TYPES[kind]


async def get_calendar(
    session: AsyncSession, *, kind: CalendarKind | None = None, limit: int, offset: int
) -> CalendarResponse:
    today = datetime.now(tz=UTC).date()  # Python-side, NOT SQL CURRENT_DATE
    governing = _calendar_governing_cte(name="governing", release_types=_kind_types(kind))
    # The noise cuts that keep a public listing clean. They are the public route's alone: the
    # my-films calendar next door deliberately carries none of them (D-1411.2).
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


async def get_my_films_calendar(
    session: AsyncSession,
    *,
    user_id: UUID,
    kind: CalendarKind | None = None,
    limit: int,
    offset: int,
) -> CalendarResponse:
    """`GET /me/calendar`: the **my films calendar** (D-34, D-39) — the release calendar
    narrowed to the films this user follows.

    Same response shape, same date-paging, same buckets, same governing-date rule and the same
    upcoming-only window as `get_calendar` — the frontend renders both tabs through one
    component, so the only thing allowed to differ is which films.

    That set is the `.ics` feed's, not the public listing's, and for the feed's reasons:

    - **Title follows, and only title follows** (EF-14): the films the user asked for by name,
      in any state and at any age. Following a director puts **nothing** here — an entity
      follow delivers that entity's attachment cards, not a place on a date list (EF-3), and a
      director's back catalogue arriving on the user's calendar is precisely what the cutover
      removed. There is no set that subtracts either: the way a film leaves this page is
      unfollowing it, which takes it off the `.ics` feed in the same breath.
    - **No popularity, runtime or adult cut.** Those keep noise off a public listing. A film the
      user followed by name is not noise to them, and applying the cuts here would make this
      page disagree with the same user's subscribed calendar — the bug this endpoint exists to
      prevent (D-1411.2). Only the slug rule survives, for the reason every surface applies it:
      a row links to the film's page and there is no page to link.

    The window is `get_calendar`'s `>= today`, *not* the feed's reach into the past (D-1411.1).
    That reach exists for a client reason — a subscribed calendar drops every event a feed stops
    publishing — and a JSON page re-rendered on every visit has no such client; paging
    soonest-first over a past window would open page one on releases a year gone.
    """
    today = datetime.now(tz=UTC).date()  # Python-side, NOT SQL CURRENT_DATE
    governing = _calendar_governing_cte(
        name="my_films_governing", title_follow_user_id=user_id, release_types=_kind_types(kind)
    )
    return await _calendar_page(
        session,
        governing=governing,
        visible=_my_films_visible(governing, today=today),
        limit=limit,
        offset=offset,
    )


def _my_films_visible(governing: CTE, *, today: date) -> tuple[ColumnElement[bool], ...]:
    """The my-films calendar's cuts over its governing CTE: upcoming, and a page to link to.

    Spelled once because the digest's slate (`digest_sender.load_slate`, FB-26) is this page
    over a 30-day window, and a slate whose cuts drifted from the page's would name a date the
    calendar does not — or miss one it does."""
    return (governing.c.governing_date >= today, Film.slug.is_not(None))


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

    - **Title follows, and only title follows** (EF-14): the films this user asked for by
      name, in any state and at any age — the same set `/me/calendar` draws, because the two
      are one surface in two formats and a feed that disagreed with the page would be the bug
      D-1411.2 exists to prevent. A followed director contributes nothing: an entity follow
      delivers cards, not dates (EF-3). Unfollowing a film is what takes it off this feed and
      off `/me/calendar` together.
    - **No popularity, runtime or adult cut.** Those keep noise off a public listing. A film the
      user followed by name is not noise to them — the same reasoning
      `digest_sender.load_slate` records for the slate.
    - **No upcoming-only filter**, unlike `get_calendar` — but a bounded reach backwards. A
      subscribed feed is the client's whole view of this calendar: a client re-fetching it drops
      every event the feed stopped publishing, so filtering to future dates would quietly erase
      each release from the user's calendar the day after it happened. Everything future is
      therefore published in full, and the past is cut at `ICAL_PAST_WINDOW_DAYS`. The cut is
      what keeps the document bounded by something other than the follow graph: an imported
      library runs to thousands of films (D-15, D-16), each with up to three buckets, and this
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

    # The governing date per (film, bucket): the earliest row in the subject, collapsed exactly
    # as `get_calendar` collapses it (NEU-1206), over the same displayable types in the same
    # single region — the one the home-release bucket is displayable in at all (D-26). Both
    # calendar kinds, unsplit: one subscription carries the theatrical arc and the digital date
    # (D-1542.4).
    governing = (
        select(
            FilmReleaseDate.film_id.label("film_id"),
            FilmReleaseDate.release_type.label("release_type"),
            func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)).label(
                "governing_date"
            ),
        )
        .where(
            FilmReleaseDate.film_id.in_(title_follow_film_ids(user_id)),
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
    # the history of the entire catalog to read a handful of followed films out of it. This
    # way each row is one index seek on `ix_catalog_film_release_date_change_lookup`, and the
    # work is bounded by how many films the user follows.
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
