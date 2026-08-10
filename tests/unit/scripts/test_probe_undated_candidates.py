import pytest

from scripts.probe_undated_candidates import (
    REPORT_COLUMNS,
    CandidateTally,
    report_row,
    seed_attachments,
    tally_attachments,
)
from tests.fixtures.tmdb import make_credit_entry, make_details, make_person_movie_credits
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails, TMDBPersonMovieCredits


def _credits(person_id: int, **kwargs) -> TMDBPersonMovieCredits:
    return TMDBPersonMovieCredits.model_validate(make_person_movie_credits(person_id, **kwargs))


def test_seed_attachments_keeps_director_writer_and_top_five_cast():
    credits = _credits(
        7,
        cast=[make_credit_entry(100, order=4)],
        crew=[
            make_credit_entry(101, department="Directing", job="Director"),
            make_credit_entry(102, department="Writing", job="Writer"),
            make_credit_entry(103, department="Writing", job="Screenplay"),
        ],
    )

    assert {(a.tmdb_id, a.role) for a in seed_attachments(7, credits)} == {
        (100, "cast"),
        (101, "director"),
        (102, "writer"),
        (103, "writer"),
    }


@pytest.mark.parametrize(
    "entry",
    [
        {"order": 5},  # billed outside the top five
        {"order": None},  # unbilled
    ],
)
def test_seed_attachments_drops_cast_below_the_top_five(entry):
    credits = _credits(7, cast=[make_credit_entry(100, **entry)])

    assert seed_attachments(7, credits) == []


@pytest.mark.parametrize("job", ["Thanks", "Producer", "Executive Producer", "Original Music"])
def test_seed_attachments_drops_non_seed_grade_crew_jobs(job):
    credits = _credits(7, crew=[make_credit_entry(100, department="Crew", job=job)])

    assert seed_attachments(7, credits) == []


def test_seed_attachments_drops_dated_films():
    credits = _credits(
        7,
        crew=[
            make_credit_entry(100, job="Director", release_date="2027-05-01"),
            make_credit_entry(101, job="Director"),
        ],
    )

    assert [a.tmdb_id for a in seed_attachments(7, credits)] == [101]


def test_seed_attachments_carries_the_seed_person():
    credits = _credits(7, crew=[make_credit_entry(100, job="Director")])

    assert [a.person_id for a in seed_attachments(7, credits)] == [7]


def test_tally_counts_distinct_people_not_credits():
    # One person credited as both director and writer is a single attachment.
    attachments = [
        *seed_attachments(
            7,
            _credits(
                7,
                crew=[
                    make_credit_entry(100, job="Director"),
                    make_credit_entry(100, job="Writer"),
                ],
            ),
        ),
        *seed_attachments(8, _credits(8, cast=[make_credit_entry(100, order=1)])),
    ]

    tally = tally_attachments(attachments)[100]
    assert tally.seed_attachment_count == 2
    assert tally.roles == {"director", "writer", "cast"}


def test_tally_groups_by_film():
    attachments = [
        *seed_attachments(7, _credits(7, crew=[make_credit_entry(100, job="Director")])),
        *seed_attachments(8, _credits(8, crew=[make_credit_entry(101, job="Director")])),
    ]

    tallies = tally_attachments(attachments)
    assert sorted(tallies) == [100, 101]
    assert all(t.seed_attachment_count == 1 for t in tallies.values())


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
