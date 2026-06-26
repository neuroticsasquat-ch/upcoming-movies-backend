"""Typed DTOs for the TMDB payloads we consume. `extra="ignore"` keeps us tolerant
of TMDB's many fields we don't use, so the API growing never breaks parsing."""

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _empty_to_none(v: Any) -> Any:
    # TMDB returns "" rather than null for unknown release dates (e.g. unannounced films).
    if v == "":
        return None
    return v


OptionalDate = Annotated[date | None, BeforeValidator(_empty_to_none)]
OptionalDatetime = Annotated[datetime | None, BeforeValidator(_empty_to_none)]


class TMDBGenre(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str


class TMDBProductionCompany(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    logo_path: str | None = None
    origin_country: str | None = None


class TMDBProductionCountry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    iso_3166_1: str
    name: str


class TMDBSpokenLanguage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    iso_639_1: str
    english_name: str
    name: str


class TMDBCollection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    poster_path: str | None = None
    backdrop_path: str | None = None


class TMDBReleaseDate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    certification: str | None = None
    iso_639_1: str | None = None
    note: str | None = None
    release_date: OptionalDatetime = None
    type: int


class TMDBReleaseDatesByCountry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    iso_3166_1: str
    release_dates: list[TMDBReleaseDate]


class TMDBReleaseDates(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[TMDBReleaseDatesByCountry]


class TMDBAlternativeTitle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    iso_3166_1: str | None = None
    title: str
    type: str | None = None


class TMDBAlternativeTitles(BaseModel):
    model_config = ConfigDict(extra="ignore")

    titles: list[TMDBAlternativeTitle] = Field(default_factory=list)


class TMDBMovieSummary(BaseModel):
    """A movie as it appears in a `/discover/movie` results list."""

    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    original_title: str | None = None
    release_date: OptionalDate = None
    overview: str | None = None
    poster_path: str | None = None
    popularity: float | None = None
    original_language: str | None = None


class TMDBMovieDetails(TMDBMovieSummary):
    """A movie from `/movie/{id}` — adds the fields only the details endpoint returns."""

    status: str | None = None
    imdb_id: str | None = None
    adult: bool | None = None
    backdrop_path: str | None = None
    budget: int | None = None
    homepage: str | None = None
    revenue: int | None = None
    runtime: int | None = None
    tagline: str | None = None
    video: bool | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    origin_country: list[str] = Field(default_factory=list)
    genres: list[TMDBGenre] = Field(default_factory=list)
    production_companies: list[TMDBProductionCompany] = Field(default_factory=list)
    production_countries: list[TMDBProductionCountry] = Field(default_factory=list)
    spoken_languages: list[TMDBSpokenLanguage] = Field(default_factory=list)
    belongs_to_collection: TMDBCollection | None = None
    release_dates: TMDBReleaseDates | None = None
    alternative_titles: TMDBAlternativeTitles | None = None
    # Populated by the client post-validation with the verbatim /movie/{id} payload, so
    # we can persist fields we don't model and backfill later without re-ingesting.
    tmdb_raw: dict[str, Any] = Field(default_factory=dict)


class TMDBDiscoverResponse(BaseModel):
    """The paged envelope returned by `/discover/movie`."""

    model_config = ConfigDict(extra="ignore")

    page: int
    results: list[TMDBMovieSummary] = Field(default_factory=list)
    total_pages: int
    total_results: int
