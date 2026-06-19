"""Builders for TMDB-shaped payloads used by the ingestion tests."""

from typing import Any


def make_details(tmdb_id: int, **overrides: Any) -> dict[str, Any]:
    """A `/movie/{id}` details payload with sensible defaults."""
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
