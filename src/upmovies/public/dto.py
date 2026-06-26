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
    occurred_at: datetime
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


class FilmDetailResponse(BaseModel):
    slug: str
    title: str
    release_date: date | None
    release_year: int | None
    poster_path: str | None
    arc_stage: str
    events: list[EventOut]
    release_dates: list[ReleaseDateOut] = []


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
    poster_path: str | None
    day: date
    top_event_type: str
    event_count: int


class FeedDayResponse(BaseModel):
    items: list[FeedDayItem]
    total: int
    limit: int
    offset: int
