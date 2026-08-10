"""The seed-grade role rules, which decide both who is a seed person and which of their
credits can admit a film. Moved here from the probe (NEU-1073) now that the sweep owns
them — one definition of seed grade, or the probe measures a different sweep than the one
that runs.
"""

import pytest

from tests.fixtures.tmdb import make_credit_entry, make_person_movie_credits
from upmovies.ingest.sweep import seed_attachments, tally_attachments
from upmovies.ingest.tmdb.schemas import TMDBPersonMovieCredits


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
    """The role is judged on the candidate film, so a "Special Thanks" credit cannot drag
    in someone's short film on the strength of the directing credit that made them a seed."""
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
