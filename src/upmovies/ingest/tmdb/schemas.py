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


class TMDBCastMember(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    credit_id: str
    original_name: str | None = None
    profile_path: str | None = None
    known_for_department: str | None = None
    gender: int | None = None
    popularity: float | None = None
    character: str | None = None
    order: int | None = None


class TMDBCrewMember(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    credit_id: str
    original_name: str | None = None
    profile_path: str | None = None
    known_for_department: str | None = None
    gender: int | None = None
    popularity: float | None = None
    department: str | None = None
    job: str | None = None


class TMDBCredits(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cast: list[TMDBCastMember] = Field(default_factory=list)
    crew: list[TMDBCrewMember] = Field(default_factory=list)


class TMDBMovieSummary(BaseModel):
    """A movie as it appears in a `/discover/movie` or `/search/movie` results list.

    `popularity` is what breaks ties between search hits that match a title equally well
    (`ingest.tmdb.resolution`), and `original_title` is the half of the match that catches a
    non-English film exported under its original name — neither is incidental here."""

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
    credits: TMDBCredits | None = None
    # Populated by the client post-validation with the verbatim /movie/{id} payload, so
    # we can persist fields we don't model and backfill later without re-ingesting.
    tmdb_raw: dict[str, Any] = Field(default_factory=dict)


class TMDBPersonMovieCastCredit(TMDBMovieSummary):
    """A film in a person's `/person/{id}/movie_credits` cast list, carrying the billing
    they held on *that* film."""

    credit_id: str | None = None
    character: str | None = None
    order: int | None = None


class TMDBPersonMovieCrewCredit(TMDBMovieSummary):
    """A film in a person's `/person/{id}/movie_credits` crew list, carrying the job they
    held on *that* film."""

    credit_id: str | None = None
    department: str | None = None
    job: str | None = None


class TMDBPersonMovieCredits(BaseModel):
    """A whole filmography in one request. Unlike `/discover/movie` this enumerates
    undated films, but the entries are summaries — no `status`, no `runtime` — so
    judging one still costs a `/movie/{id}` fetch."""

    model_config = ConfigDict(extra="ignore")

    id: int
    cast: list[TMDBPersonMovieCastCredit] = Field(default_factory=list)
    crew: list[TMDBPersonMovieCrewCredit] = Field(default_factory=list)


class TMDBDiscoverResponse(BaseModel):
    """The paged envelope returned by `/discover/movie`."""

    model_config = ConfigDict(extra="ignore")

    page: int
    results: list[TMDBMovieSummary] = Field(default_factory=list)
    total_pages: int
    total_results: int


class TMDBSearchResponse(TMDBDiscoverResponse):
    """The paged envelope returned by `/search/movie`.

    Identical in shape to discover's, and a subclass rather than a second copy of four fields:
    TMDB documents one envelope for both, so a future field belongs on both. Distinct from it
    by name because the two endpoints answer different questions and a caller reading
    `TMDBDiscoverResponse` back from a search would have to check which."""


# The v3 user-authorization payloads (D-16). TMDB's approve flow is three endpoints that each
# answer one field wrapped in a `success` envelope; they are modelled rather than read out of
# the dict so the client keeps its promise that callers parse nothing.


class TMDBRequestToken(BaseModel):
    """`/authentication/token/new` — the token the user approves on themoviedb.org."""

    model_config = ConfigDict(extra="ignore")

    success: bool
    request_token: str
    expires_at: str | None = None


class TMDBSessionResponse(BaseModel):
    """`/authentication/session/new` — an approved request token exchanged for a session id.

    That session id can read *and write* the user's TMDB account, which is why nothing in this
    codebase stores one: the import holds it in memory and deletes it in a `finally` (D-16)."""

    model_config = ConfigDict(extra="ignore")

    success: bool
    session_id: str


class TMDBAccount(BaseModel):
    """`/account` — who the session belongs to.

    Only the two fields the import needs: the numeric id the watchlist and favorites are read
    under, and the username the job row records so the UI can say "Imported from @user"."""

    model_config = ConfigDict(extra="ignore")

    id: int
    username: str


class TMDBAccountMoviesResponse(TMDBDiscoverResponse):
    """The paged envelope returned by `/account/{id}/watchlist/movies` and
    `/account/{id}/favorite/movies`.

    Discover's envelope again, and a subclass for the same reason `TMDBSearchResponse` is one:
    TMDB documents one paged shape and a future field belongs on all of them. Named apart
    because these two endpoints are the only ones here that answer for a *person* rather than
    for the catalog, and a caller reading a discover response back from them would have to
    check which."""


# `/search/person` — the first half of the D-21 candidate union. A hit carries the person
# fields `catalog.person` stores plus the two the deterministic scorer reads directly:
# `known_for_department` (department vs the role the article gives them) and `popularity`
# (tiebreak only). `known_for` supplies the filmography-overlap feature without a second
# request per candidate, which for a ten-candidate shortlist is ten requests saved.


class TMDBKnownForTitle(BaseModel):
    """One entry in a person search hit's `known_for` list.

    TMDB mixes films and television here and names them differently — a film has
    `title`/`original_title`, a show has `name`/`original_name` — so both pairs are modelled
    and `display_title` picks whichever the entry carries. Parsing TV entries as movies would
    fail validation on the missing `title` and lose the whole hit, and dropping them would
    hide an overlap the article may well be naming."""

    model_config = ConfigDict(extra="ignore")

    id: int
    media_type: str | None = None
    title: str | None = None
    name: str | None = None
    original_title: str | None = None
    original_name: str | None = None

    @property
    def display_title(self) -> str | None:
        """The entry's title in its own language-neutral field, film or show."""
        return self.title or self.name

    @property
    def display_original_title(self) -> str | None:
        """The entry's original-language title, which is how a non-English production is
        named in a trade story that does not use its English release title."""
        return self.original_title or self.original_name


class TMDBPersonSearchHit(BaseModel):
    """A person as `/search/person` returns them (D-21).

    The person fields are the same set `catalog.person` holds, which is what lets an accepted
    hit go through `ingest.tmdb.upsert.upsert_people` unchanged rather than through a second
    write that would have to be kept agreeing with it."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    original_name: str | None = None
    profile_path: str | None = None
    known_for_department: str | None = None
    gender: int | None = None
    popularity: float | None = None
    known_for: list[TMDBKnownForTitle] = Field(default_factory=list)


class TMDBPersonSearchResponse(BaseModel):
    """The paged envelope returned by `/search/person`.

    The same four envelope fields as `TMDBDiscoverResponse` but not a subclass of it: the
    results are people, and narrowing an inherited `list[TMDBMovieSummary]` to a different
    type is the one thing a subclass cannot honestly do."""

    model_config = ConfigDict(extra="ignore")

    page: int
    results: list[TMDBPersonSearchHit] = Field(default_factory=list)
    total_pages: int
    total_results: int


class TMDBPersonDetails(BaseModel):
    """A person as `/person/{id}` returns them — the only TMDB endpoint that carries birth and
    death dates (NEU-1370).

    Neither credits endpoint the sweep already calls returns them, which is the whole reason
    this request exists; it is made lazily, once per person, and only for people a credit
    event is about to name. The other four fields are the overlap with `catalog.person`, kept
    so the one fetch refreshes what it has seen rather than writing dates beside a stale
    `popularity` it just received a fresher value for.

    `birthday`/`deathday` go through `OptionalDate` because TMDB answers `""` as readily as
    `null` for a person it holds no date for, and a living person is indistinguishable here
    from one whose death nobody has recorded — see `catalog.person.deathday`."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    birthday: OptionalDate = None
    deathday: OptionalDate = None
    popularity: float | None = None
    profile_path: str | None = None
    known_for_department: str | None = None


# `/movie/{id}/watch/providers` — who is carrying the film, per region (D-27). TMDB sources
# this from JustWatch, whose terms require the attribution rendered wherever these names are.


class TMDBWatchProvider(BaseModel):
    """One provider offering a film under one monetization type.

    `provider_id` and not `id`: this is the shape TMDB returns, and the id space is JustWatch's
    provider catalogue rather than TMDB's own — `catalog.watch_provider` stores it under `id`
    because there it is the table's own key."""

    model_config = ConfigDict(extra="ignore")

    provider_id: int
    provider_name: str
    logo_path: str | None = None
    display_priority: int | None = None


class TMDBWatchProviderRegion(BaseModel):
    """One region's offers: the JustWatch deep link TMDB hands out, and the provider lists per
    monetization type.

    Only the three types D-27 tracks are modelled. TMDB also returns `ads` and `free`, which
    are deliberately dropped rather than folded into `flatrate`: an ad-supported tier is a
    different claim about how a viewer watches the film, and `now_available` (D-28) cards a
    first sighting per monetization type — so folding them would card a beat the product does
    not mean. A missing key and an empty list are the same thing here, so all three default
    empty."""

    model_config = ConfigDict(extra="ignore")

    link: str | None = None
    flatrate: list[TMDBWatchProvider] = Field(default_factory=list)
    rent: list[TMDBWatchProvider] = Field(default_factory=list)
    buy: list[TMDBWatchProvider] = Field(default_factory=list)


class TMDBWatchProviders(BaseModel):
    """The whole `/movie/{id}/watch/providers` payload: the film's id and a region-keyed map.

    Every region is parsed, not just the one v1 polls. The endpoint answers for all of them in
    one response — there is no per-region request to save by narrowing here — and the region
    cut is the caller's (`PRIMARY_REGION`), the same way it is for release dates."""

    model_config = ConfigDict(extra="ignore")

    id: int
    results: dict[str, TMDBWatchProviderRegion] = Field(default_factory=dict)
