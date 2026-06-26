from datetime import date, datetime

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


# --- release_dates DTO tests ---


def test_movie_details_parses_nested_release_dates():
    payload = {
        "id": 550,
        "title": "Fight Club",
        "release_dates": {
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {
                            "certification": "R",
                            "iso_639_1": "en",
                            "note": "",
                            "release_date": "1999-10-15T00:00:00.000Z",
                            "type": 3,
                        }
                    ],
                }
            ]
        },
    }
    details = TMDBMovieDetails.model_validate(payload)
    assert details.release_dates is not None
    assert len(details.release_dates.results) == 1
    country = details.release_dates.results[0]
    assert country.iso_3166_1 == "US"
    assert len(country.release_dates) == 1
    rd = country.release_dates[0]
    assert rd.certification == "R"
    assert rd.type == 3
    assert isinstance(rd.release_date, datetime)
    assert rd.release_date.year == 1999


def test_movie_details_release_dates_absent_is_none():
    details = TMDBMovieDetails.model_validate({"id": 1, "title": "Minimal"})
    assert details.release_dates is None


def test_release_date_entry_with_empty_string_date_parses_as_none():
    """TMDB returns release_date: "" for unannounced entries — should parse to None, not crash."""
    payload = {
        "id": 999,
        "title": "TBA Film",
        "release_dates": {
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {
                            "certification": "NR",
                            "iso_639_1": "en",
                            "note": "",
                            "release_date": "",
                            "type": 3,
                        },
                        {
                            "certification": "R",
                            "iso_639_1": "en",
                            "note": "",
                            "release_date": "2026-07-15T00:00:00.000Z",
                            "type": 4,
                        },
                    ],
                }
            ]
        },
    }
    details = TMDBMovieDetails.model_validate(payload)
    assert details.release_dates is not None
    country = details.release_dates.results[0]
    assert len(country.release_dates) == 2
    assert country.release_dates[0].release_date is None
    assert isinstance(country.release_dates[1].release_date, datetime)


# --- alternative_titles DTO tests ---


def test_movie_details_parses_nested_alternative_titles():
    """Parsing a nested alternative_titles payload populates the typed tree."""
    payload = {
        "id": 550,
        "title": "Fight Club",
        "alternative_titles": {
            "titles": [
                {
                    "iso_3166_1": "DE",
                    "title": "Fight Club - Der Film",
                    "type": "",
                },
                {
                    "iso_3166_1": "FR",
                    "title": "Fight Club FR",
                    "type": "Dubbed title",
                },
            ]
        },
    }
    details = TMDBMovieDetails.model_validate(payload)
    assert details.alternative_titles is not None
    assert len(details.alternative_titles.titles) == 2
    first = details.alternative_titles.titles[0]
    assert first.iso_3166_1 == "DE"
    assert first.title == "Fight Club - Der Film"
    second = details.alternative_titles.titles[1]
    assert second.iso_3166_1 == "FR"
    assert second.type == "Dubbed title"


def test_movie_details_alternative_titles_absent_is_none():
    """A film with no alternative_titles key leaves the field None."""
    details = TMDBMovieDetails.model_validate({"id": 1, "title": "Minimal"})
    assert details.alternative_titles is None


def test_alternative_titles_ignores_unknown_extra_keys():
    """Extra keys in alternative_titles payload are silently ignored."""
    payload = {
        "id": 550,
        "title": "Fight Club",
        "alternative_titles": {
            "titles": [
                {
                    "iso_3166_1": "US",
                    "title": "Fight Club",
                    "type": None,
                    "extra_unknown_key": "should be ignored",
                }
            ],
            "id": 550,  # TMDB sometimes embeds the movie id here
        },
    }
    details = TMDBMovieDetails.model_validate(payload)
    assert details.alternative_titles is not None
    assert details.alternative_titles.titles[0].iso_3166_1 == "US"
    assert details.alternative_titles.titles[0].title == "Fight Club"


def test_movie_details_release_dates_ignores_unknown_extra_keys():
    payload = {
        "id": 550,
        "title": "Fight Club",
        "unknown_future_key": "ignored",
        "release_dates": {
            "results": [
                {
                    "iso_3166_1": "GB",
                    "release_dates": [
                        {
                            "release_date": "2000-01-01T00:00:00.000Z",
                            "type": 5,
                            "extra_key_from_tmdb": "should be ignored",
                        }
                    ],
                    "another_extra_key": 42,
                }
            ],
            "id": 550,  # TMDB sometimes embeds the movie id here
        },
    }
    details = TMDBMovieDetails.model_validate(payload)
    assert details.release_dates is not None
    assert details.release_dates.results[0].iso_3166_1 == "GB"
    assert details.release_dates.results[0].release_dates[0].type == 5
