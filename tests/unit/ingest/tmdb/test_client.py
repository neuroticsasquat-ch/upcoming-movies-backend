import time

import httpx
import pytest
import respx

from tests.fixtures.tmdb import make_details
from upmovies.ingest.tmdb.client import RateLimiter, TMDBClient
from upmovies.ingest.tmdb.schemas import TMDBCredits, TMDBDiscoverResponse, TMDBMovieDetails

BASE_URL = "https://api.themoviedb.org/3"


def _client(retry_max_attempts: int = 3) -> TMDBClient:
    return TMDBClient(
        base_url=BASE_URL,
        api_key="test-key",
        rate_calls=20,
        rate_window=1,
        retry_max_attempts=retry_max_attempts,
        retry_base_delay=0.01,
    )


async def test_rate_limiter_enforces_rate():
    limiter = RateLimiter(calls=3, window_seconds=1)
    start = time.monotonic()
    for _ in range(6):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0, f"6 calls at 3/s should take >= 1s, took {elapsed:.3f}s"


@respx.mock
async def test_discover_movies_parses_response_and_sends_api_key_and_page():
    route = respx.get(f"{BASE_URL}/discover/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "page": 1,
                "total_pages": 3,
                "total_results": 60,
                "results": [{"id": 1, "title": "One"}, {"id": 2, "title": "Two"}],
            },
        )
    )
    async with _client() as c:
        result = await c.discover_movies(page=1, sort_by="popularity.desc")

    assert isinstance(result, TMDBDiscoverResponse)
    assert result.total_pages == 3
    assert [m.id for m in result.results] == [1, 2]
    params = route.calls.last.request.url.params
    assert params.get("api_key") == "test-key"
    assert params.get("page") == "1"
    assert params.get("sort_by") == "popularity.desc"


@respx.mock
async def test_discover_movies_paginates():
    respx.get(f"{BASE_URL}/discover/movie", params={"page": "2"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "page": 2,
                "total_pages": 3,
                "total_results": 60,
                "results": [{"id": 3, "title": "Three"}],
            },
        )
    )
    async with _client() as c:
        result = await c.discover_movies(page=2)
    assert result.page == 2
    assert result.results[0].id == 3


@respx.mock
async def test_movie_details_parses_status_and_imdb_id():
    respx.get(f"{BASE_URL}/movie/27205").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 27205,
                "title": "Inception",
                "status": "Released",
                "imdb_id": "tt1375666",
                "release_date": "2010-07-15",
            },
        )
    )
    async with _client() as c:
        details = await c.movie_details(27205)
    assert isinstance(details, TMDBMovieDetails)
    assert details.status == "Released"
    assert details.imdb_id == "tt1375666"


@respx.mock
async def test_client_retries_on_5xx_then_succeeds():
    route = respx.get(f"{BASE_URL}/movie/42").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"id": 42, "title": "ok"}),
        ]
    )
    async with _client() as c:
        details = await c.movie_details(42)
    assert details.id == 42
    assert route.call_count == 3


@respx.mock
async def test_client_honors_retry_after_on_429():
    route = respx.get(f"{BASE_URL}/movie/7").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": 7, "title": "ok"}),
        ]
    )
    async with _client() as c:
        details = await c.movie_details(7)
    assert details.id == 7
    assert route.call_count == 2


@respx.mock
async def test_client_raises_after_exhausting_retries_on_5xx():
    respx.get(f"{BASE_URL}/movie/500").mock(return_value=httpx.Response(503))
    async with _client(retry_max_attempts=2) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.movie_details(500)


@respx.mock
async def test_client_does_not_retry_on_404():
    route = respx.get(f"{BASE_URL}/movie/9999").mock(return_value=httpx.Response(404))
    async with _client() as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.movie_details(9999)
    assert route.call_count == 1


@respx.mock
async def test_movie_details_captures_raw_payload_including_unmodeled_keys():
    payload = {
        "id": 27205,
        "title": "Inception",
        "status": "Released",
        "imdb_id": "tt1375666",
        "genres": [{"id": 28, "name": "Action"}],
        "production_code": "ABC-123",  # a key our DTO does not model
    }
    respx.get(f"{BASE_URL}/movie/27205").mock(return_value=httpx.Response(200, json=payload))
    async with _client() as c:
        details = await c.movie_details(27205)
    assert details.tmdb_raw == payload
    assert details.tmdb_raw["production_code"] == "ABC-123"
    assert [g.id for g in details.genres] == [28]


@respx.mock
async def test_movie_details_sends_append_to_response_with_release_dates():
    """movie_details must issue exactly one request with release_dates in append_to_response."""
    route = respx.get(f"{BASE_URL}/movie/27205").mock(
        return_value=httpx.Response(200, json=make_details(27205))
    )
    async with _client() as c:
        await c.movie_details(27205)

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    atr = params.get("append_to_response", "")
    assert "release_dates" in atr.split(",")
    # The request-level append_to_response must not clobber the client's default api_key auth.
    assert params.get("api_key") == "test-key"


@respx.mock
async def test_movie_details_sends_append_to_response_with_alternative_titles():
    """movie_details must issue one request with alternative_titles in append_to_response."""
    route = respx.get(f"{BASE_URL}/movie/27205").mock(
        return_value=httpx.Response(200, json=make_details(27205))
    )
    async with _client() as c:
        await c.movie_details(27205)

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    atr = params.get("append_to_response", "")
    assert "alternative_titles" in atr.split(",")


@respx.mock
async def test_movie_details_parses_appended_alternative_titles_block_and_captures_in_raw():
    """When the response includes an appended alternative_titles block, it must parse into
    details.alternative_titles and also be present verbatim in details.tmdb_raw."""
    alternative_titles_block = {
        "titles": [
            {
                "iso_3166_1": "DE",
                "title": "Inception - Der Film",
                "type": "",
            }
        ]
    }
    payload = make_details(27205, alternative_titles=alternative_titles_block)
    respx.get(f"{BASE_URL}/movie/27205").mock(return_value=httpx.Response(200, json=payload))
    async with _client() as c:
        details = await c.movie_details(27205)

    # Parsed into typed DTO
    assert details.alternative_titles is not None
    assert details.alternative_titles.titles[0].iso_3166_1 == "DE"
    assert details.alternative_titles.titles[0].title == "Inception - Der Film"
    # Also present in raw payload
    assert details.tmdb_raw["alternative_titles"] == alternative_titles_block


@respx.mock
async def test_movie_details_sends_append_to_response_with_credits():
    """movie_details must issue exactly one request with credits in append_to_response."""
    route = respx.get(f"{BASE_URL}/movie/27205").mock(
        return_value=httpx.Response(200, json=make_details(27205))
    )
    async with _client() as c:
        await c.movie_details(27205)

    assert route.call_count == 1
    params = route.calls.last.request.url.params
    atr = params.get("append_to_response", "")
    assert "credits" in atr.split(",")


@respx.mock
async def test_movie_details_parses_appended_credits_block_and_captures_in_raw():
    """When the response includes an appended credits block, it must parse into
    details.credits (TMDBCredits) and also be present verbatim in details.tmdb_raw."""
    credits_block = {
        "cast": [
            {
                "id": 6193,
                "name": "Leonardo DiCaprio",
                "credit_id": "52fe4251c3a36847f8014199",
                "character": "Cobb",
                "order": 0,
            }
        ],
        "crew": [
            {
                "id": 525,
                "name": "Christopher Nolan",
                "credit_id": "52fe4251c3a36847f8014201",
                "department": "Directing",
                "job": "Director",
            }
        ],
    }
    payload = make_details(27205, credits=credits_block)
    respx.get(f"{BASE_URL}/movie/27205").mock(return_value=httpx.Response(200, json=payload))
    async with _client() as c:
        details = await c.movie_details(27205)

    # Parsed into typed DTO
    assert isinstance(details.credits, TMDBCredits)
    assert len(details.credits.cast) == 1
    assert details.credits.cast[0].name == "Leonardo DiCaprio"
    assert details.credits.cast[0].character == "Cobb"
    assert len(details.credits.crew) == 1
    assert details.credits.crew[0].job == "Director"
    # Also present in raw payload
    assert details.tmdb_raw["credits"] == credits_block


@respx.mock
async def test_movie_details_parses_appended_release_dates_block_and_captures_in_raw():
    """When the response includes an appended release_dates block, it must parse into
    details.release_dates and also be present verbatim in details.tmdb_raw."""
    release_dates_block = {
        "results": [
            {
                "iso_3166_1": "US",
                "release_dates": [
                    {
                        "certification": "PG-13",
                        "iso_639_1": "",
                        "note": "",
                        "release_date": "2010-07-16T00:00:00.000Z",
                        "type": 3,
                    }
                ],
            }
        ]
    }
    payload = make_details(27205, release_dates=release_dates_block)
    respx.get(f"{BASE_URL}/movie/27205").mock(return_value=httpx.Response(200, json=payload))
    async with _client() as c:
        details = await c.movie_details(27205)

    # Parsed into typed DTO
    assert details.release_dates is not None
    assert details.release_dates.results[0].iso_3166_1 == "US"
    assert details.release_dates.results[0].release_dates[0].type == 3
    # Also present in raw payload
    assert details.tmdb_raw["release_dates"] == release_dates_block
