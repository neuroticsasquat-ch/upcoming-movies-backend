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


def test_movie_details_parses_full_nested_payload():
    details = TMDBMovieDetails.model_validate(
        {
            "id": 11,
            "title": "Star Wars",
            "original_title": "Star Wars",
            "release_date": "1977-05-25",
            "status": "Released",
            "imdb_id": "tt0076759",
            "adult": False,
            "backdrop_path": "/backdrop.jpg",
            "budget": 11000000,
            "homepage": "http://www.starwars.com",
            "revenue": 775398007,
            "runtime": 121,
            "tagline": "A long time ago...",
            "video": False,
            "vote_average": 8.2,
            "vote_count": 22061,
            "popularity": 20.69,
            "original_language": "en",
            "origin_country": ["US"],
            "genres": [{"id": 12, "name": "Adventure"}, {"id": 28, "name": "Action"}],
            "production_companies": [
                {"id": 1, "logo_path": "/l.png", "name": "Lucasfilm Ltd.", "origin_country": "US"}
            ],
            "production_countries": [{"iso_3166_1": "US", "name": "United States of America"}],
            "spoken_languages": [{"english_name": "English", "iso_639_1": "en", "name": "English"}],
            "belongs_to_collection": {
                "id": 10,
                "name": "Star Wars Collection",
                "poster_path": "/p.jpg",
                "backdrop_path": "/b.jpg",
            },
        }
    )
    assert details.budget == 11000000
    assert details.runtime == 121
    assert details.origin_country == ["US"]
    assert [g.id for g in details.genres] == [12, 28]
    assert details.production_companies[0].name == "Lucasfilm Ltd."
    assert details.production_companies[0].origin_country == "US"
    assert details.production_countries[0].iso_3166_1 == "US"
    assert details.spoken_languages[0].iso_639_1 == "en"
    assert details.belongs_to_collection is not None
    assert details.belongs_to_collection.id == 10


def test_movie_details_defaults_nested_collections_when_absent():
    details = TMDBMovieDetails.model_validate({"id": 1, "title": "Minimal"})
    assert details.genres == []
    assert details.production_companies == []
    assert details.production_countries == []
    assert details.spoken_languages == []
    assert details.origin_country == []
    assert details.belongs_to_collection is None
    assert details.tmdb_raw == {}
