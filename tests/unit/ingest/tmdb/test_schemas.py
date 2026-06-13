from datetime import date

from upmovies.ingest.tmdb.schemas import (
    TMDBDiscoverResponse,
    TMDBMovieDetails,
    TMDBMovieSummary,
)


def test_movie_summary_parses_discover_result():
    movie = TMDBMovieSummary.model_validate(
        {
            "id": 27205,
            "title": "Inception",
            "original_title": "Inception",
            "release_date": "2010-07-15",
            "overview": "A thief who steals corporate secrets...",
            "poster_path": "/poster.jpg",
            "popularity": 123.45,
            "original_language": "en",
            "vote_average": 8.3,  # extra field we don't consume
        }
    )
    assert movie.id == 27205
    assert movie.title == "Inception"
    assert movie.release_date == date(2010, 7, 15)
    assert movie.original_language == "en"


def test_movie_summary_coerces_empty_release_date_to_none():
    movie = TMDBMovieSummary.model_validate({"id": 1, "title": "TBA", "release_date": ""})
    assert movie.release_date is None


def test_movie_summary_allows_missing_optional_fields():
    movie = TMDBMovieSummary.model_validate({"id": 1, "title": "Minimal"})
    assert movie.original_title is None
    assert movie.poster_path is None
    assert movie.popularity is None


def test_movie_details_includes_status_and_imdb_id():
    details = TMDBMovieDetails.model_validate(
        {
            "id": 27205,
            "title": "Inception",
            "status": "Released",
            "imdb_id": "tt1375666",
            "release_date": "2010-07-15",
        }
    )
    assert details.status == "Released"
    assert details.imdb_id == "tt1375666"
    assert details.release_date == date(2010, 7, 15)


def test_discover_response_parses_envelope_and_results():
    payload = {
        "page": 2,
        "total_pages": 500,
        "total_results": 10000,
        "results": [
            {"id": 1, "title": "One"},
            {"id": 2, "title": "Two"},
        ],
    }
    resp = TMDBDiscoverResponse.model_validate(payload)
    assert resp.page == 2
    assert resp.total_pages == 500
    assert resp.total_results == 10000
    assert [m.id for m in resp.results] == [1, 2]
    assert isinstance(resp.results[0], TMDBMovieSummary)
