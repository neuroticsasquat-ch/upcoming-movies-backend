"""The probe's report contract — the columns M4's tuning reads. The seed-grade rules the
probe applies moved to `upmovies.ingest.sweep.seeds` (NEU-1077) and are tested there.
"""

from scripts.probe_undated_candidates import REPORT_COLUMNS, CandidateTally, report_row
from tests.fixtures.tmdb import make_details
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails


def test_report_row_matches_the_column_contract():
    tally = CandidateTally(tmdb_id=100, seed_person_ids={7, 8}, roles={"cast", "director"})
    details = TMDBMovieDetails.model_validate(
        make_details(
            100,
            title="Untitled Project",
            status="Planned",
            original_language="fr",
            popularity=0.62,
            runtime=0,
            release_date="",
        )
    )

    row = report_row(tally, details)

    assert list(row) == list(REPORT_COLUMNS)
    assert row == {
        "tmdb_id": 100,
        "title": "Untitled Project",
        "status": "Planned",
        "original_language": "fr",
        "popularity": 0.62,
        "seed_attachment_count": 2,
        "seed_roles_matched": "director|cast",
        "runtime": 0,
    }


def test_report_row_orders_roles_by_seed_grade_not_alphabetically():
    tally = CandidateTally(tmdb_id=100, seed_person_ids={7}, roles={"writer", "cast", "director"})
    details = TMDBMovieDetails.model_validate(make_details(100, release_date=""))

    assert report_row(tally, details)["seed_roles_matched"] == "director|writer|cast"


def test_report_row_tolerates_missing_details_fields():
    tally = CandidateTally(tmdb_id=100, seed_person_ids={7}, roles={"director"})
    details = TMDBMovieDetails.model_validate({"id": 100, "title": "Untitled"})

    row = report_row(tally, details)
    assert row["status"] is None
    assert row["runtime"] is None
    assert row["popularity"] is None


def test_report_row_prefers_the_details_title():
    # The credits list can carry a stale working title; /movie/{id} is canonical.
    tally = CandidateTally(tmdb_id=100, title="Old Working Title", seed_person_ids={7})
    details = TMDBMovieDetails.model_validate(make_details(100, title="Real Title"))

    assert report_row(tally, details)["title"] == "Real Title"
