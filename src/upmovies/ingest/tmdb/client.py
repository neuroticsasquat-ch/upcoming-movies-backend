"""Async TMDB v3 client with sliding-window rate limiting and bounded retry/backoff.
Self-contained (no DB); callers parse nothing — methods return typed DTOs."""

import asyncio
import time
from collections import deque
from typing import Any

import httpx

from upmovies.config import Settings
from upmovies.ingest.tmdb.schemas import (
    TMDBAccount,
    TMDBAccountMoviesResponse,
    TMDBDiscoverResponse,
    TMDBMovieDetails,
    TMDBMovieSummary,
    TMDBPersonMovieCredits,
    TMDBPersonSearchHit,
    TMDBPersonSearchResponse,
    TMDBRequestToken,
    TMDBSearchResponse,
    TMDBSessionResponse,
)
from upmovies.logging_config import redact_api_key


class TMDBNotFound(httpx.HTTPStatusError):
    """TMDB has no entry at this id — a 404, not a transport or server failure.

    Its own type because the two mean opposite things to a pipeline. A 5xx or a timeout is an
    *outage*: retrying later is right, and a run of them should abort the pass before it burns
    thousands of requests. A 404 is *terminal* — the entry is gone from TMDB and no retry
    brings it back — so counting one toward a consecutive-failure guard reads a permanent
    condition as a temporary one. That is the 2026-08-11 sweep incident (NEU-1124): eleven
    deleted ids sorted to the head of a stalest-first queue tripped an abort built for an
    outage.

    Subclasses `httpx.HTTPStatusError` so every existing `except httpx.HTTPError` still
    catches it unchanged; a caller that wants to tell the two apart puts a narrower clause
    ahead of its own.
    """


class TMDBAuthRejected(httpx.HTTPStatusError):
    """TMDB refused a request token at `/authentication/session/new` (D-16).

    The user's half of the approve flow failing — they closed the page without approving, the
    token expired at TMDB's end, or it has already been spent — and therefore a 400 to whoever
    called the callback route, not a server fault. Its own type for the same reason
    `TMDBNotFound` is one: the caller needs to tell a condition it can report from one it can
    only log."""


TMDB_INVALID_API_KEY = 7
"""TMDB's own `status_code` for a dead API key, which it also answers 401 for.

Read so that the one 401 meaning *our deployment is broken* is not reported to a user as
"you did not approve" — they would retry the approve screen forever against a key that is the
actual fault. Every other 401 from that endpoint is the token."""


def _is_rejected_token(resp: httpx.Response) -> bool:
    """Whether a failed `/authentication/session/new` is the token's fault rather than ours."""
    if resp.status_code != httpx.codes.UNAUTHORIZED:
        return False
    try:
        body = resp.json()
    except ValueError:
        return True
    return not (isinstance(body, dict) and body.get("status_code") == TMDB_INVALID_API_KEY)


class RateLimiter:
    """Sliding-window token bucket. Allows up to `calls` calls per `window_seconds`."""

    def __init__(self, calls: int, window_seconds: float):
        self._calls = calls
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._calls:
                wait = self._window - (now - self._timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                    while self._timestamps and now - self._timestamps[0] >= self._window:
                        self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


_SHARED_LIMITERS: dict[tuple[int, float], RateLimiter] = {}


def _shared_limiter(calls: int, window: float) -> RateLimiter:
    """The process's one limiter for these limits, created on first ask.

    Keyed rather than a bare singleton so a test that builds a client at its own tiny window
    cannot leave that window behind for whatever runs next (NEU-1399).
    """
    key = (calls, window)
    if key not in _SHARED_LIMITERS:
        _SHARED_LIMITERS[key] = RateLimiter(calls, window)
    return _SHARED_LIMITERS[key]


def reset_shared_limiters() -> None:
    """Drop every shared window. Tests only."""
    _SHARED_LIMITERS.clear()


class TMDBClient:
    """Async context manager over httpx. The v3 API key is sent as the `api_key`
    query param on every request via the client's default params.

    **Build production clients with `TMDBClient.from_settings(settings)`**, which hands them
    the process-wide limiter so every concurrent consumer shares one TMDB budget. The raw
    constructor gives the client a window of its own — that is for tests, and for a caller
    that deliberately wants an independent budget. `tests/unit/ingest/tmdb/test_client.py`
    pins the production call sites to the classmethod, because a new one built the old way
    would silently reintroduce the doubling (NEU-1399).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        rate_calls: int,
        rate_window: float,
        retry_max_attempts: int = 5,
        retry_base_delay: float = 0.5,
        timeout: float = 30.0,
        *,
        limiter: RateLimiter | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._limiter = limiter or RateLimiter(rate_calls, rate_window)
        self._retry_max = retry_max_attempts
        self._retry_base = retry_base_delay
        self._client = httpx.AsyncClient(timeout=timeout, params={"api_key": api_key})

    @classmethod
    def from_settings(cls, settings: Settings) -> "TMDBClient":
        """Build a client sharing this process's TMDB window.

        Sharing is defined as *clients built from the app settings share* — the actual intent —
        rather than *clients whose numbers happen to match*, which would make test isolation
        rest on no two suites picking the same limits.
        """
        return cls(
            base_url=settings.tmdb_base_url,
            api_key=settings.tmdb_api_key,
            rate_calls=settings.tmdb_rate_limit_requests,
            rate_window=settings.tmdb_rate_limit_window_seconds,
            retry_max_attempts=settings.tmdb_retry_max_attempts,
            limiter=_shared_limiter(
                settings.tmdb_rate_limit_requests,
                settings.tmdb_rate_limit_window_seconds,
            ),
        )

    async def __aenter__(self) -> "TMDBClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            await self._limiter.acquire()
            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt + 1 >= self._retry_max:
                    raise
                await asyncio.sleep(self._retry_base * (2**attempt))
                attempt += 1
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = (
                    float(retry_after)
                    if retry_after is not None
                    else self._retry_base * (2**attempt)
                )
                await asyncio.sleep(wait)
                continue  # 429 does not count against the retry budget

            if 500 <= resp.status_code < 600:
                if attempt + 1 >= self._retry_max:
                    self._raise_for_status(resp)
                await asyncio.sleep(self._retry_base * (2**attempt))
                attempt += 1
                continue

            self._raise_for_status(resp)
            return resp

    def _raise_for_status(self, resp: httpx.Response) -> None:
        """`resp.raise_for_status()`, but with the API key scrubbed and 404 given its own type.

        httpx builds its message from the full request URL, which carries `api_key=` — so the
        stock call leaks a live credential into every log line that formats the exception. The
        message is composed here instead, at the one point where a key-bearing URL can enter an
        exception at all (NEU-1124).
        """
        if not resp.is_error:
            return
        url = redact_api_key(str(resp.request.url))
        message = f"{resp.status_code} {resp.reason_phrase} for url '{url}'"
        error = TMDBNotFound if resp.status_code == httpx.codes.NOT_FOUND else httpx.HTTPStatusError
        raise error(message, request=resp.request, response=resp)

    async def discover_movies(self, *, page: int = 1, **params: str | int) -> TMDBDiscoverResponse:
        """Page through `/discover/movie`. Extra keyword args are passed through as
        query params (e.g. sort_by, primary_release_date.gte, with_release_type)."""
        url = f"{self._base_url}/discover/movie"
        resp = await self._request("GET", url, params={"page": page, **params})
        return TMDBDiscoverResponse.model_validate(resp.json())

    async def search_movie(self, query: str, year: int | None = None) -> list[TMDBMovieSummary]:
        """Search `/search/movie` by title, optionally narrowed to a release year.

        First page only — twenty hits. The caller (`ingest.tmdb.resolution`) matches on an
        exact folded title, so a film that is not in the first page under its own name is not
        going to be matched on the second either; paging would double the request count of an
        import for hits no rule can accept.

        `primary_release_year` rather than `year`: the two are different filters at TMDB, and
        this one asks about the film's first release, which is the date the search results
        themselves carry. `year` matches a release in *any* country in that year, which for a
        back-catalogue film matches a re-release and would let an import place a 1977 title on
        a 2020 restoration."""
        url = f"{self._base_url}/search/movie"
        params: dict[str, str | int] = {"query": query, "page": 1}
        if year is not None:
            params["primary_release_year"] = year
        resp = await self._request("GET", url, params=params)
        return TMDBSearchResponse.model_validate(resp.json()).results

    async def search_person(self, query: str) -> list[TMDBPersonSearchHit]:
        """Search `/search/person` by name — the first candidate source for person
        resolution (D-21).

        First page only — twenty hits — and for a firmer reason than `search_movie`'s: the
        resolver caps its whole candidate union at ten, and the union also includes the film's
        current credits and its recent change stream. A name whose right person is on page two
        of TMDB's own relevance ranking is not going to survive that cap, so the second page
        would cost a request per mention to widen a shortlist that is then truncated.

        No `year`-style narrowing exists here, so nothing is passed beyond the query: TMDB
        ranks person hits by its own popularity signal, which the scorer re-reads from
        `popularity` as a tiebreak rather than trusting as an ordering (D-21)."""
        url = f"{self._base_url}/search/person"
        resp = await self._request("GET", url, params={"query": query, "page": 1})
        return TMDBPersonSearchResponse.model_validate(resp.json()).results

    async def movie_details(self, tmdb_id: int) -> TMDBMovieDetails:
        """Fetch full details for a single movie from `/movie/{id}`. Attaches the verbatim
        JSON as `tmdb_raw` so the caller can persist fields we don't model."""
        url = f"{self._base_url}/movie/{tmdb_id}"
        resp = await self._request(
            "GET", url, params={"append_to_response": "credits,release_dates,alternative_titles"}
        )
        data = resp.json()
        details = TMDBMovieDetails.model_validate(data)
        details.tmdb_raw = data
        return details

    async def person_movie_credits(self, person_id: int) -> TMDBPersonMovieCredits:
        """Fetch a person's whole movie filmography from `/person/{id}/movie_credits` —
        one request, undated entries included."""
        url = f"{self._base_url}/person/{person_id}/movie_credits"
        resp = await self._request("GET", url)
        return TMDBPersonMovieCredits.model_validate(resp.json())

    # --- v3 user authorization and the account lists (D-16) ---------------------------------
    #
    # The only endpoints here that act for a *user* rather than for the catalog. They are on
    # this client rather than a second one because they want the same window, the same retry
    # policy and the same key-scrubbing: an import competing with the daily pass for TMDB's
    # budget is exactly what the shared limiter exists to arbitrate (NEU-1399).

    async def create_request_token(self) -> str:
        """A fresh request token for the approve flow. The user approves it on themoviedb.org;
        `create_session` then exchanges it."""
        url = f"{self._base_url}/authentication/token/new"
        resp = await self._request("GET", url)
        return TMDBRequestToken.model_validate(resp.json()).request_token

    async def create_session(self, request_token: str) -> str:
        """Exchange an **approved** request token for a session id.

        `TMDBAuthRejected` when TMDB refuses the token — not approved, already spent, or
        expired at their end. That is the user's half of the flow failing and is a 400 to the
        caller, which is why it is told apart from the 401 that means *our* API key is dead:
        the second is a deployment fault and must not be reported as "you did not approve"."""
        url = f"{self._base_url}/authentication/session/new"
        try:
            resp = await self._request("POST", url, json={"request_token": request_token})
        except httpx.HTTPStatusError as e:
            if _is_rejected_token(e.response):
                raise TMDBAuthRejected(str(e), request=e.request, response=e.response) from e
            raise
        return TMDBSessionResponse.model_validate(resp.json()).session_id

    async def account(self, session_id: str) -> TMDBAccount:
        """Whose account this session reads — the id the lists below are fetched under."""
        url = f"{self._base_url}/account"
        resp = await self._request("GET", url, params={"session_id": session_id})
        return TMDBAccount.model_validate(resp.json())

    async def account_watchlist_movies(
        self, account_id: int, session_id: str, *, limit: int | None = None
    ) -> list[TMDBMovieSummary]:
        """Every movie on this account's TMDB watchlist, paged."""
        return await self._account_movies(
            f"/account/{account_id}/watchlist/movies", session_id, limit=limit
        )

    async def account_favorite_movies(
        self, account_id: int, session_id: str, *, limit: int | None = None
    ) -> list[TMDBMovieSummary]:
        """Every movie this account has marked favorite, paged."""
        return await self._account_movies(
            f"/account/{account_id}/favorite/movies", session_id, limit=limit
        )

    async def _account_movies(
        self, path: str, session_id: str, *, limit: int | None
    ) -> list[TMDBMovieSummary]:
        """Page through one of the account lists until TMDB runs out of pages, or `limit` rows
        have been collected.

        `limit` is the caller's row cap reaching down into the paging rather than being applied
        to the result, so an account with a five-figure watchlist costs the pages the import
        will actually use instead of every page and then a slice."""
        movies: list[TMDBMovieSummary] = []
        page = 1
        while True:
            resp = await self._request(
                "GET",
                f"{self._base_url}{path}",
                params={"session_id": session_id, "page": page},
            )
            payload = TMDBAccountMoviesResponse.model_validate(resp.json())
            movies.extend(payload.results)
            if limit is not None and len(movies) >= limit:
                return movies[:limit]
            if page >= payload.total_pages or not payload.results:
                return movies
            page += 1

    async def delete_session(self, session_id: str) -> None:
        """Invalidate a session id at TMDB. The last thing an import does, in a `finally`:
        nothing here stores the credential, so this is the only thing that ends it (D-16)."""
        url = f"{self._base_url}/authentication/session"
        await self._request("DELETE", url, json={"session_id": session_id})
