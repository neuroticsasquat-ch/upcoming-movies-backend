"""Async TMDB v3 client with sliding-window rate limiting and bounded retry/backoff.
Self-contained (no DB); callers parse nothing — methods return typed DTOs."""

import asyncio
import time
from collections import deque
from typing import Any

import httpx

from upmovies.ingest.tmdb.schemas import (
    TMDBDiscoverResponse,
    TMDBMovieDetails,
    TMDBPersonMovieCredits,
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


class TMDBClient:
    """Async context manager over httpx. The v3 API key is sent as the `api_key`
    query param on every request via the client's default params."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        rate_calls: int,
        rate_window: float,
        retry_max_attempts: int = 5,
        retry_base_delay: float = 0.5,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(rate_calls, rate_window)
        self._retry_max = retry_max_attempts
        self._retry_base = retry_base_delay
        self._client = httpx.AsyncClient(timeout=timeout, params={"api_key": api_key})

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
