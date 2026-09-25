from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from upmovies.app.dto import HeadlineReleaseOut


class SourceOut(BaseModel):
    url: str
    source: str
    title: str
    published_at: datetime | None


class EventOut(BaseModel):
    event_id: UUID
    event_type: str
    confidence: str
    created_at: datetime
    # When the beat itself happened, as against `created_at` when we carded it. Ordering is
    # unaffected on either surface — `created_at` stays the feed axis (ADR-0016) and the film
    # page still orders by `occurred_at` (NEU-1204). This ships it so the card can disclose a
    # "first seen" line (D-9).
    occurred_at: datetime
    summary: str
    summary_edited: bool
    # "story" | "catalog". A `catalog` event was created by a TMDB field or credit change with
    # no story behind it, so `sources` may legitimately be empty and the card attributes to
    # TMDB in place of outlets (ADR-0014).
    provenance: str
    # "published" | "superseded", and the event that supersedes this one (D-2). A superseded
    # card still renders in place; the client marks it and links to `superseded_by` rather
    # than dropping it.
    status: str
    superseded_by: UUID | None
    # The YouTube key of the trailer this card is about, for a card that embeds a player
    # (D-35). Set only on a `trailer` event the video poll raised from a video it can name;
    # None on every other event, and on a story-born trailer card, where the outlets reported
    # a trailer but we hold no video to play.
    video_key: str | None
    sources: list[SourceOut]


class EntityEventsResponse(BaseModel):
    """A page of one entity's own cards (EF-18), newest first.

    The same `EventOut` the feed and the film page carry, so an entity page renders a card with
    the component they already use rather than a second one that drifts.

    **Keyset-paginated, not offset-paginated**, and so with no `total`: the list grows at the
    top while it is read — a studio's next attachment lands above whatever the visitor is
    looking at — and an offset would push unread cards past the boundary. `next_cursor` is null
    on the last page, which is how a caller knows it has reached the end.
    """

    items: list[EventOut]
    next_cursor: str | None


class FilmIndexItem(BaseModel):
    ref: str
    title: str
    release_year: int | None
    poster_path: str | None
    arc_stage: str


class FilmIndexResponse(BaseModel):
    items: list[FilmIndexItem]
    total: int
    limit: int
    offset: int


class PersonSearchItem(BaseModel):
    """A follow target from `catalog.person`. `id` is TMDB's person id; stringified, it is
    the `entity_id` `POST /me/follows` takes for `entity_type=person`."""

    id: int
    name: str
    known_for_department: str | None
    profile_path: str | None


class PersonSearchResponse(BaseModel):
    items: list[PersonSearchItem]
    total: int
    limit: int
    offset: int


class PopularPeopleResponse(BaseModel):
    """The onboarding grid (D-17): a capped list, not a page — there is no `total` or
    `offset` because the grid never scrolls past the first `limit` faces."""

    items: list[PersonSearchItem]
    limit: int


class CompanySearchItem(BaseModel):
    """`id` is TMDB's company id; stringified, it is the `entity_id` `POST /me/follows` takes
    for `entity_type=company`."""

    id: int
    name: str
    logo_path: str | None
    origin_country: str | None


class CompanySearchResponse(BaseModel):
    items: list[CompanySearchItem]
    total: int
    limit: int
    offset: int


class CollectionSearchItem(BaseModel):
    """`id` is TMDB's collection id; stringified, it is the `entity_id` `POST /me/follows`
    takes for `entity_type=franchise`."""

    id: int
    name: str
    poster_path: str | None


class CollectionSearchResponse(BaseModel):
    items: list[CollectionSearchItem]
    total: int
    limit: int
    offset: int


class ReleaseDateOut(BaseModel):
    country: str
    release_type: int
    type_label: str
    date: datetime
    certification: str | None


class FilmRowOut(BaseModel):
    """One film cited on an entity page — person, studio or franchise.

    The row shape `catalog.headline_release` feeds: enough of the film to render it without a
    second request per item, and the film page for the rest. It was the watchlist row's shape
    before EF-14 retired the watchlist, and it is unchanged by that — the same fields, cited the
    same way, so a film on an entity page and the same film anywhere else cannot show different
    dates.

    There is deliberately no `release_date`: it used to be `catalog.film.release_date`, TMDB's
    primary date, which the film page never displays — so a row could cite a date that the page
    it links to did not show (NEU-1397). `headline_release` is the displayable answer, and it is
    null only for a film with no displayable release row and no primary date.

    `ref` is the film page's URL segment, carried rather than rebuilt: a client assembling it
    from `tmdb_id` and `slug` is a client that gets it wrong for a film with no slug.

    The studio and franchise pages (NEU-1428) return this row **bare**; the person page wraps
    it in `PersonFilmOut` to hang that person's credits off it, which is the only thing the
    three pages do differently."""

    id: UUID
    tmdb_id: int
    slug: str | None
    title: str
    poster_path: str | None
    headline_release: HeadlineReleaseOut | None
    ref: str


class PersonCreditOut(BaseModel):
    """One credit a person holds on one film.

    A writer-director holds two of these on the same film; they are listed rather than folded,
    because "Director · Writer" is what the page reads. `credit_order` is TMDB's 0-indexed
    billing and is null for crew and for an unbilled cast entry.

    **No `tier` since EF-1.** A follow is binary and reaches every credit, so there is no cut
    for a badge to name — every row here is one a follow of this person delivers."""

    credit_type: Literal["cast", "crew"]
    job: str | None
    character: str | None
    credit_order: int | None


class PersonFilmOut(BaseModel):
    """One film on a person's page, with every credit they hold on it.

    Every row is reached by a follow of this person (EF-2), so the row carries no tier of its
    own any more — the list *is* what following them delivers."""

    film: FilmRowOut
    credits: list[PersonCreditOut]


class CompanyDetailResponse(BaseModel):
    """A studio's page (EF-17): who they are, and the films a follow could reach.

    Same two lists as `PersonDetailResponse` and for the same reason — `upcoming` is the
    in-play set, `recent` is the alert window less it, and the back catalogue is absent by
    design. The rows are bare `FilmRowOut`s: a studio credit has no job to name, so the person
    row's `credits` would be empty ceremony on every row.

    `id` is the TMDB company id; stringified, it is the `entity_id` a `company` follow is keyed
    on. `ref` is canonical — the client redirects when the one it asked with differs.
    """

    ref: str
    id: int
    name: str
    logo_path: str | None
    upcoming: list[FilmRowOut]
    recent: list[FilmRowOut]


class CollectionDetailResponse(BaseModel):
    """A franchise's page (EF-17) — `CompanyDetailResponse` over `catalog.collection`, carrying
    the collection's `poster_path` where a studio carries its `logo_path`.

    `id` is the TMDB collection id; stringified, it is the `entity_id` a `franchise` follow is
    keyed on.
    """

    ref: str
    id: int
    name: str
    poster_path: str | None
    upcoming: list[FilmRowOut]
    recent: list[FilmRowOut]


class PersonDetailResponse(BaseModel):
    """A person's page (D-1416.6): who they are, and the films a follow could reach.

    **Upcoming and recently released only.** `upcoming` is the in-play set and `recent` is the
    alert window less the in-play set, which between them are exactly what a follow can reach
    (D-46) — so the page shows the user what following this person would get them and nothing
    else. A film is in one list or the other, never both, and their back catalogue is absent by
    design rather than by pagination.

    `id` is the TMDB person id; stringified, it is the `entity_id` a `person` follow is keyed
    on. `ref` is canonical — the client redirects when the one it asked with differs, as the
    film page does."""

    ref: str
    id: int
    name: str
    profile_path: str | None
    known_for_department: str | None
    birthday: date | None
    deathday: date | None
    upcoming: list[PersonFilmOut]
    recent: list[PersonFilmOut]


class CollectionOut(BaseModel):
    # TMDB collection id, which is the `franchise` entity id in the follow graph (D-10).
    id: int
    name: str
    poster_path: str | None = None


class CompanyOut(BaseModel):
    # TMDB company id, the `company` entity id in the follow graph (D-10).
    id: int
    name: str


class CastMemberOut(BaseModel):
    # TMDB person id (= `catalog.person`'s PK), the `person` entity id (D-10).
    person_id: int
    name: str
    character: str | None
    profile_path: str | None


class CrewMemberOut(BaseModel):
    person_id: int  # see CastMemberOut.person_id
    name: str
    job: str | None
    department: str | None


class ProviderOut(BaseModel):
    """One service carrying a film, as TMDB (sourcing JustWatch) names it."""

    # TMDB's `provider_id` — JustWatch's id space. Exposed so a client can key its own logo
    # cache on it; it is not a follow-graph entity and no route accepts it.
    id: int
    name: str
    logo_path: str | None = None


class WhereToWatchOut(BaseModel):
    """The current US where-to-watch box (D-29) — a snapshot, never a history.

    Bucketed by how a reader pays rather than by service, because that is the decision the box
    answers: a subscription already covers `flatrate`, `rent` and `buy` cost money today. Each
    bucket is always present, empty when nobody offers the film that way, so a client can render
    one section without guarding three keys. The whole object is `None` when nobody carries the
    film at all — see `FilmDetailResponse.where_to_watch`.

    **`attribution` and `link` are terms, not decoration.** TMDB's terms for
    `/movie/{id}/watch/providers` require crediting JustWatch wherever the data renders and
    linking back to TMDB's own watch page. `attribution` is a `Literal`, so it is fixed at
    "JustWatch" and cannot be set to anything else by a caller assembling this model; `link` is
    nullable only because TMDB itself omits it for some regions.
    """

    region: str
    flatrate: list[ProviderOut] = []
    rent: list[ProviderOut] = []
    buy: list[ProviderOut] = []
    link: str | None = None
    attribution: Literal["JustWatch"] = "JustWatch"


class DayGroup(BaseModel):
    day: date
    heading: str
    news_events: list[EventOut]
    tmdb_events: list[EventOut]


class FilmDetailResponse(BaseModel):
    ref: str
    # `catalog.film`'s UUID — the `title` entity id the follow graph keys on (D-10). Opaque,
    # and every route that accepts it is behind cookie auth, CSRF
    # and `require_entitled()`, so exposing it on this public endpoint grants nothing.
    id: UUID
    title: str
    tmdb_id: int
    imdb_id: str | None = None
    release_date: date | None
    release_year: int | None
    poster_path: str | None
    arc_stage: str
    day_groups: list[DayGroup]
    release_dates: list[ReleaseDateOut] = []
    overview: str | None = None
    tagline: str | None = None
    runtime: int | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    original_language: str | None = None
    backdrop_path: str | None = None
    genres: list[str] = []
    # Display forms (already abbreviated), sorted by display name. The film page lists these in
    # its spec sheet; it reads directors from `crew` instead, so no `directors` field here.
    production_countries: list[str] = []
    production_companies: list[str] = []
    # The same companies as `production_companies`, carrying the id a `company` follow needs.
    # A new field beside the names rather than a widening of them, so that the frontend and this
    # service deploy independently in either order with no flag day.
    companies: list[CompanyOut] = []
    collection: CollectionOut | None = None
    alternative_titles: list[str] = []
    cast: list[CastMemberOut] = []
    crew: list[CrewMemberOut] = []
    # `None` — not an empty box — when no poll has found the film anywhere (D-29). The two are
    # different answers: an empty box would claim we looked and it is nowhere, which is only
    # true for a film the providers poll actually reaches (D-27's scoped set is films past
    # their theatrical date, plus anything somebody's follows cover).
    where_to_watch: WhereToWatchOut | None = None


class FeedItem(BaseModel):
    film_ref: str
    film_title: str
    event_type: str
    confidence: str
    occurred_at: datetime
    created_at: datetime
    summary: str
    provenance: str  # see EventOut.provenance
    sources: list[SourceOut]


class FeedResponse(BaseModel):
    items: list[FeedItem]
    total: int
    limit: int
    offset: int


class FeedDayItem(BaseModel):
    film_ref: str
    film_title: str
    release_year: int | None
    poster_path: str | None
    # Rendered only when the film has no country, director, or year — the last resort that
    # keeps a bare title from reading as a rendering bug (NEU-1085, narrowed by NEU-1215). It
    # has to ride along on the row: it is not derivable client-side from release_year.
    arc_stage: str
    # The other two elements of the title parenthetical, both display-ready and never None.
    # Countries hold display forms sorted by display name; directors hold person names ordered
    # by billing. Neither is capped here — capping is presentation and differs per surface.
    production_countries: list[str] = []
    directors: list[str] = []
    day: date
    top_event_type: str
    # Every distinct beat this film-day carries, most-significant first — so `event_types[0]`
    # is always `top_event_type`. The feed labels the whole set inline after the title
    # (NEU-1212) as a fallback on a row that arrives with no events; the lead type alone can't
    # express it, since a day that pairs a trailer with a casting beat reads as trailer-only
    # otherwise.
    event_types: list[str]
    event_count: int
    # True when *any* of this film-day's visible events has a linked story — i.e. a news
    # outlet reported some part of the day's activity (NEU-1137). Derived from
    # EXISTS(event_story), NOT from `provenance`: provenance records where an event was born
    # and is never mutated when a story attaches later, so a TMDB-carded beat a trade covers
    # afterwards would otherwise stay filed under TMDB forever. Classified by "any" because
    # the row is one (film, day): a film is never listed twice under one date heading, and
    # `event_count`/`top_event_type` stay computed over all of the day's events rather than
    # over the section this row lands in.
    news_backed: bool
    # The actual events on this (film, day), with their summaries and sources, matching the
    # EventOut shape used on the film detail page. A catalog row ships its catalog events
    # like a news row ships its news events, just with empty `sources` (NEU-1467 reversed
    # NEU-1208's empty list).
    events: list[EventOut] = []


class FeedDayResponse(BaseModel):
    items: list[FeedDayItem]
    total: int
    limit: int
    offset: int


class CalendarItem(BaseModel):
    film_ref: str
    film_title: str
    release_year: int | None
    poster_path: str | None
    release_date: date  # US release date → "YYYY-MM-DD"
    release_type: str  # display bucket: "limited" | "wide" | "digital" | "physical"
    director: str | None  # credited director(s), joined with ", "; null when none
    stars: list[str]  # first 3 billed cast names
    genres: list[str]  # up to 3 genre names, ordered by name


class CalendarResponse(BaseModel):
    items: list[CalendarItem]
    total: int
    limit: int
    offset: int
