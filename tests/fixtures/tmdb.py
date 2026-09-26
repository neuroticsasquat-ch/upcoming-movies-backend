"""Builders for TMDB-shaped payloads used by the ingestion tests."""

from typing import Any


def make_details(
    tmdb_id: int,
    *,
    release_dates: dict[str, Any] | None = None,
    alternative_titles: dict[str, Any] | None = None,
    credits: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A `/movie/{id}` details payload with sensible defaults.

    Pass ``release_dates`` to include an appended release_dates block (as TMDB returns
    when ``append_to_response=release_dates`` is set). Pass ``alternative_titles`` to
    include an appended alternative_titles block. Pass ``credits`` to include an appended
    credits block. All other fields can be overridden via keyword arguments.
    """
    payload: dict[str, Any] = {
        "id": tmdb_id,
        "title": f"Movie {tmdb_id}",
        "original_title": f"Movie {tmdb_id}",
        "release_date": "2026-07-15",
        "status": "Released",
        "overview": f"Overview for {tmdb_id}",
        "poster_path": f"/poster{tmdb_id}.jpg",
        "imdb_id": f"tt{tmdb_id:07d}",
        "popularity": 50.0,
        "original_language": "en",
        "adult": False,
        "backdrop_path": f"/backdrop{tmdb_id}.jpg",
        "budget": 1_000_000,
        "homepage": f"https://example.com/{tmdb_id}",
        "revenue": 5_000_000,
        "runtime": 120,
        "tagline": f"Tagline {tmdb_id}",
        "video": False,
        "vote_average": 7.5,
        "vote_count": 100,
        "origin_country": ["US"],
        "genres": [{"id": 28, "name": "Action"}, {"id": 12, "name": "Adventure"}],
        "production_companies": [
            {"id": 1, "name": "Lucasfilm Ltd.", "logo_path": "/logo.png", "origin_country": "US"}
        ],
        "production_countries": [{"iso_3166_1": "US", "name": "United States of America"}],
        "spoken_languages": [{"english_name": "English", "iso_639_1": "en", "name": "English"}],
        "belongs_to_collection": None,
    }
    if release_dates is not None:
        payload["release_dates"] = release_dates
    if alternative_titles is not None:
        payload["alternative_titles"] = alternative_titles
    if credits is not None:
        payload["credits"] = credits
    payload.update(overrides)
    return payload


def make_discover_page(
    *,
    page: int,
    total_pages: int,
    results: list[dict[str, Any]],
    total_results: int | None = None,
) -> dict[str, Any]:
    """A `/discover/movie` envelope. Each result only needs `id`/`popularity` for the
    service, which uses discover purely to enumerate candidate ids + gate on popularity."""
    return {
        "page": page,
        "total_pages": total_pages,
        "total_results": total_results if total_results is not None else len(results),
        "results": results,
    }


def make_summary(tmdb_id: int, popularity: float = 50.0, **overrides: Any) -> dict[str, Any]:
    """A single `/discover/movie` result row."""
    row: dict[str, Any] = {
        "id": tmdb_id,
        "title": f"Movie {tmdb_id}",
        "popularity": popularity,
    }
    row.update(overrides)
    return row


def make_person_movie_credits(
    person_id: int,
    *,
    cast: list[dict[str, Any]] | None = None,
    crew: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A `/person/{id}/movie_credits` envelope. Entries are movie summaries carrying the
    role the person held on that film — `order`/`character` for cast, `department`/`job`
    for crew — and no `status`, which is why the probe still needs a details fetch."""
    return {"id": person_id, "cast": cast or [], "crew": crew or []}


def make_credit_entry(tmdb_id: int, **overrides: Any) -> dict[str, Any]:
    """One row inside a `/person/{id}/movie_credits` cast/crew list. Undated by default —
    TMDB sends `""` rather than null for a film with no announced release date."""
    row: dict[str, Any] = {
        "id": tmdb_id,
        "title": f"Movie {tmdb_id}",
        "original_title": f"Movie {tmdb_id}",
        "release_date": "",
        "popularity": 0.4,
        "original_language": "en",
        "credit_id": f"credit-{tmdb_id}",
    }
    row.update(overrides)
    return row


def make_person_search_hit(person_id: int, **overrides: Any) -> dict[str, Any]:
    """One `/search/person` result row, with the `known_for` block TMDB attaches."""
    row: dict[str, Any] = {
        "id": person_id,
        "name": f"Person {person_id}",
        "original_name": f"Person {person_id}",
        "profile_path": f"/profile{person_id}.jpg",
        "known_for_department": "Acting",
        "gender": 2,
        "popularity": 12.5,
        "adult": False,  # extra field we don't consume
        "known_for": [
            {
                "id": 1000 + person_id,
                "media_type": "movie",
                "title": f"Known For {person_id}",
                "original_title": f"Known For {person_id}",
            }
        ],
    }
    row.update(overrides)
    return row


def make_person_details(person_id: int, **overrides: Any) -> dict[str, Any]:
    """A `/person/{id}` body — the only endpoint carrying `birthday` and `deathday`.

    Both default to None, which is what TMDB holds for most people: the sanity holds (D-8)
    and the resolver's age/alive feature (D-21) both read an absent date as saying nothing.
    """
    row: dict[str, Any] = {
        "id": person_id,
        "name": f"Person {person_id}",
        "birthday": None,
        "deathday": None,
        "popularity": 12.5,
        "profile_path": f"/profile{person_id}.jpg",
        "known_for_department": "Acting",
    }
    row.update(overrides)
    return row


def make_person_search_page(
    *, results: list[dict[str, Any]], page: int = 1, total_pages: int = 1
) -> dict[str, Any]:
    """A `/search/person` envelope — discover's four fields around person hits."""
    return {
        "page": page,
        "total_pages": total_pages,
        "total_results": len(results),
        "results": results,
    }


def make_provider(provider_id: int, **overrides: Any) -> dict[str, Any]:
    """One entry in a `/movie/{id}/watch/providers` offer list."""
    row: dict[str, Any] = {
        "provider_id": provider_id,
        "provider_name": f"Provider {provider_id}",
        "logo_path": f"/provider{provider_id}.jpg",
        "display_priority": 0,
    }
    row.update(overrides)
    return row


def make_watch_providers(
    tmdb_id: int,
    *,
    region: str = "US",
    flatrate: list[int] | None = None,
    rent: list[int] | None = None,
    buy: list[int] | None = None,
    link: str | None = None,
    regions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A `/movie/{id}/watch/providers` payload, given provider ids per monetization type.

    Passing no ids at all yields `{"id": ..., "results": {}}` — TMDB's answer for a film
    nobody carries anywhere, which is a 200 and not a 404. Pass ``regions`` to build the
    region map directly when a test needs more than one region or an unusual shape.
    """
    if regions is not None:
        return {"id": tmdb_id, "results": regions}
    if flatrate is None and rent is None and buy is None:
        return {"id": tmdb_id, "results": {}}
    block: dict[str, Any] = {
        "link": link if link is not None else f"https://www.themoviedb.org/movie/{tmdb_id}/watch"
    }
    for field, ids in (("flatrate", flatrate), ("rent", rent), ("buy", buy)):
        if ids:
            block[field] = [make_provider(pid) for pid in ids]
    return {"id": tmdb_id, "results": {region: block}}


def make_video(key: str, **overrides: Any) -> dict[str, Any]:
    """One entry in a `/movie/{id}/videos` result list — a YouTube trailer by default."""
    row: dict[str, Any] = {
        "id": f"tmdb-{key}",
        "iso_639_1": "en",
        "iso_3166_1": "US",
        "key": key,
        "name": "Official Trailer",
        "site": "YouTube",
        "size": 1080,
        "type": "Trailer",
        "official": True,
        "published_at": "2026-09-01T15:00:00.000Z",
    }
    row.update(overrides)
    return row


def make_videos(tmdb_id: int, videos: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A `/movie/{id}/videos` payload. No videos yields an empty `results` — TMDB's 200 for a
    film with nothing to watch yet, which is the ordinary answer for an unreleased title."""
    return {"id": tmdb_id, "results": videos or []}
