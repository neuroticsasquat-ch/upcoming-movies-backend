from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


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
    summary: str
    summary_edited: bool
    # "story" | "catalog". A `catalog` event was created by a TMDB field or credit change with
    # no story behind it, so `sources` may legitimately be empty and the card attributes to
    # TMDB in place of outlets (ADR-0014).
    provenance: str
    sources: list[SourceOut]


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


class ReleaseDateOut(BaseModel):
    country: str
    release_type: int
    type_label: str
    date: datetime
    certification: str | None


class CollectionOut(BaseModel):
    name: str
    poster_path: str | None = None


class CastMemberOut(BaseModel):
    name: str
    character: str | None
    profile_path: str | None


class CrewMemberOut(BaseModel):
    name: str
    job: str | None
    department: str | None


class DayGroup(BaseModel):
    day: date
    heading: str
    news_events: list[EventOut]
    tmdb_events: list[EventOut]


class FilmDetailResponse(BaseModel):
    ref: str
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
    production_companies: list[str] = []
    collection: CollectionOut | None = None
    alternative_titles: list[str] = []
    cast: list[CastMemberOut] = []
    crew: list[CrewMemberOut] = []


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
    # Rendered in the release year's slot for an undated film (NEU-1085), so it has to
    # ride along on the row — it is not derivable client-side from release_year.
    arc_stage: str
    day: date
    top_event_type: str
    # Every distinct beat this film-day carries, most-significant first — so `event_types[0]`
    # is always `top_event_type`. The feed labels the whole set inline after the title
    # (NEU-1212), on rows that ship no events; the lead type alone can't express it, since a
    # day that pairs a trailer with a casting beat reads as trailer-only otherwise.
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
    # EventOut shape used on the film detail page. Empty for catalog-sourced rows on the
    # grouped feed, which render as title-only links (NEU-1208).
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
    release_type: str  # display bucket: "premiere" | "limited" | "wide"
    director: str | None  # credited director(s), joined with ", "; null when none
    stars: list[str]  # first 3 billed cast names
    genres: list[str]  # up to 3 genre names, ordered by name


class CalendarResponse(BaseModel):
    items: list[CalendarItem]
    total: int
    limit: int
    offset: int
