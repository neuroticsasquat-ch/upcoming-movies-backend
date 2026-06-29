from datetime import date, datetime

from pydantic import BaseModel


class SourceOut(BaseModel):
    url: str
    source: str
    title: str
    published_at: datetime | None


class EventOut(BaseModel):
    event_type: str
    confidence: str
    created_at: datetime
    summary: str
    sources: list[SourceOut]


class FilmIndexItem(BaseModel):
    slug: str
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


class FilmDetailResponse(BaseModel):
    slug: str
    title: str
    tmdb_id: int
    imdb_id: str | None = None
    release_date: date | None
    release_year: int | None
    poster_path: str | None
    arc_stage: str
    events: list[EventOut]
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
    film_slug: str
    film_title: str
    event_type: str
    confidence: str
    occurred_at: datetime
    created_at: datetime
    summary: str
    sources: list[SourceOut]


class FeedResponse(BaseModel):
    items: list[FeedItem]
    total: int
    limit: int
    offset: int


class FeedDayItem(BaseModel):
    film_slug: str
    film_title: str
    release_year: int | None
    poster_path: str | None
    day: date
    top_event_type: str
    event_count: int


class FeedDayResponse(BaseModel):
    items: list[FeedDayItem]
    total: int
    limit: int
    offset: int


class CalendarItem(BaseModel):
    film_slug: str
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
