"""The sweep's enumerate phase end to end against a mocked TMDB: what it reaches, what it
admits (nothing, until a tranche is opened), and how it behaves when TMDB misbehaves.

The admitting-nothing case is the one that ships (NEU-1077), so it is asserted as a
*catalog* fact — no film row appears — rather than by trusting a counter.
"""

from datetime import date, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from tests.fixtures.catalog import add_credit, add_film
from tests.fixtures.tmdb import make_credit_entry, make_details, make_person_movie_credits
from upmovies.catalog.models import Film, FilmAlternativeTitle, FilmCredit, Person
from upmovies.ingest.models import IngestRun
from upmovies.ingest.sweep import (
    AdmissionTranches,
    CreditEventResult,
    FieldEventResult,
    RefreshResult,
    ReleaseEventResult,
    run_sweep_enumerate,
    sweep_detail,
)
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film

BASE_URL = "https://api.themoviedb.org/3"
EXCLUDED = frozenset({"Released", "Canceled"})
TODAY = date(2026, 8, 10)
DORMANCY_DAYS = 365
DATED = TODAY + timedelta(days=90)

DIRECTORS_ONLY = AdmissionTranches(enabled=True, directors=True)
WRITERS_ONLY = AdmissionTranches(enabled=True, writers=True)
DIRECTORS_AND_WRITERS = AdmissionTranches(enabled=True, directors=True, writers=True)
CAST_ONLY = AdmissionTranches(enabled=True, cast=True)


async def _seed_director(session, person_id: int = 10, film_tmdb_id: int = 1):
    film = await add_film(session, film_tmdb_id, release_date=DATED)
    await add_credit(
        session, film, person_id, credit_type="crew", department="Directing", job="Director"
    )
    return film


def _mock_credits(person_id: int, **kwargs):
    return respx.get(f"{BASE_URL}/person/{person_id}/movie_credits").mock(
        return_value=httpx.Response(200, json=make_person_movie_credits(person_id, **kwargs))
    )


def _mock_details(tmdb_id: int, **overrides):
    return respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(
        return_value=httpx.Response(
            200, json=make_details(tmdb_id, release_date="", status="Planned", **overrides)
        )
    )


async def _run(session_factory, tmdb_client, run_id, **overrides):
    return await run_sweep_enumerate(
        session_factory=session_factory,
        client=tmdb_client,
        run_id=run_id,
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
        **{"tranches": AdmissionTranches(), "corroboration_threshold": 1, **overrides},
    )


async def _run_row(session, run_id) -> IngestRun:
    return await session.get(IngestRun, run_id, populate_existing=True)


async def _film_count(session) -> int:
    return (await session.execute(select(func.count()).select_from(Film))).scalar_one()


@respx.mock
async def test_finds_candidates_and_admits_none_while_every_tranche_is_off(
    session, session_factory, tmdb_client, run_id
):
    await _seed_director(session)
    await session.commit()
    before = await _film_count(session)
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.seed_people == 1
    assert result.candidates_found == 1
    assert result.withheld == 1
    assert result.admitted == 0
    assert await _film_count(session) == before
    row = await _run_row(session, run_id)
    assert (row.items_processed, row.items_failed) == (0, 0)


@respx.mock
async def test_a_pass_that_admits_nothing_still_reports_itself_alive(
    session, session_factory, tmdb_client, run_id
):
    """The silent-stretch case that caused the 2026-08-11 incident (NEU-1117): with every
    tranche closed the phase records no progress at all, yet it is working. The heartbeat
    has to advance `last_progress_at` anyway, or the staleness sweep reads it as an orphan.
    """
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.admitted == 0
    row = await _run_row(session, run_id)
    assert (row.items_processed, row.items_failed) == (0, 0)
    assert row.last_progress_at is not None


@respx.mock
async def test_an_open_tranche_admits_and_counts_the_film(
    session, session_factory, tmdb_client, run_id
):
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100, title="Untitled Project")

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert (result.admitted, result.withheld) == (1, 0)
    admitted = (await session.execute(select(Film).where(Film.tmdb_id == 100))).scalar_one()
    assert admitted.title == "Untitled Project"
    assert (await _run_row(session, run_id)).items_processed == 1


@respx.mock
async def test_a_closed_tranche_withholds_a_candidate_reached_only_at_that_grade(
    session, session_factory, tmdb_client, run_id
):
    """The ramp is per seed grade: with directors open and cast still closed, a film
    reached only through a top-billed credit waits (§7.4)."""
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    _mock_credits(11, cast=[make_credit_entry(100, order=0)])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert (result.admitted, result.withheld) == (0, 1)
    assert await _film_count(session) == 1


@respx.mock
async def test_tracks_distinct_seed_people_per_candidate(
    session, session_factory, tmdb_client, run_id
):
    """`seed_attachment_count` is what the corroboration threshold will read (§4.2), so it
    counts corroborating *people* — one person credited twice is one attachment."""
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    _mock_credits(
        10,
        crew=[
            make_credit_entry(100, department="Directing", job="Director"),
            make_credit_entry(100, department="Writing", job="Writer"),
        ],
    )
    _mock_credits(11, cast=[make_credit_entry(100, order=0), make_credit_entry(101, order=1)])
    _mock_details(100)
    _mock_details(101)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.attachment_histogram == {1: 1, 2: 1}


@respx.mock
async def test_a_film_already_in_the_catalog_costs_no_details_fetch(
    session, session_factory, tmdb_client, run_id
):
    await _seed_director(session)
    await add_film(session, 500, release_date=None, status="Planned")
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(500, department="Directing", job="Director")])
    details = respx.get(f"{BASE_URL}/movie/500")

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert not details.called
    assert result.skipped_already_known == 1
    assert result.admitted == 0


@respx.mock
async def test_drops_a_candidate_the_details_reveal_to_be_dated(
    session, session_factory, tmdb_client, run_id
):
    # A credits entry can omit a release date that `/movie/{id}` carries; a dated film
    # belongs to discover, not to the sweep.
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    respx.get(f"{BASE_URL}/movie/100").mock(
        return_value=httpx.Response(
            200, json=make_details(100, status="Planned", release_date="2027-09-01")
        )
    )

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.skipped_dated_on_details == 1
    assert await _film_count(session) == 1


@pytest.mark.parametrize("status", ["Released", "Canceled"])
@respx.mock
async def test_drops_excluded_statuses_through_classify_skip(
    session, session_factory, tmdb_client, run_id, status
):
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    respx.get(f"{BASE_URL}/movie/100").mock(
        return_value=httpx.Response(200, json=make_details(100, status=status, release_date=""))
    )

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.skipped_excluded_status == 1
    assert await _film_count(session) == 1


@respx.mock
async def test_a_short_undated_film_is_not_dropped_for_its_runtime(
    session, session_factory, tmdb_client, run_id
):
    """The admission bar is status only (§4.1). Undated films rarely carry a real runtime,
    and the discover pipeline's short-film rule would reject them on a zero."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100, runtime=0)

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.admitted == 1


@respx.mock
async def test_one_unreachable_person_does_not_cost_the_rest(
    session, session_factory, tmdb_client, run_id
):
    """A 503 — TMDB is up but this request is not getting through. Contrast
    `test_a_404_person_is_missing_not_failed`: a deleted person is not a failure at all."""
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(503))
    _mock_credits(11, cast=[make_credit_entry(100, order=0)])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.person_failures == 1
    assert result.candidates_found == 1
    assert (await _run_row(session, run_id)).items_failed == 1


@respx.mock
async def test_one_unjudgeable_candidate_does_not_roll_back_an_earlier_admission(
    session, session_factory, tmdb_client, run_id
):
    """Commits are per item, so the film admitted before the failure survives it."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(
        10,
        crew=[
            make_credit_entry(100, department="Directing", job="Director"),
            make_credit_entry(101, department="Directing", job="Director"),
        ],
    )
    _mock_details(100)
    respx.get(f"{BASE_URL}/movie/101").mock(return_value=httpx.Response(500))

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.candidate_failures == 1
    assert result.admitted == 1
    assert (await session.execute(select(Film.tmdb_id).where(Film.tmdb_id == 100))).scalar_one()
    row = await _run_row(session, run_id)
    assert (row.items_processed, row.items_failed) == (1, 1)


@respx.mock
async def test_aborts_after_consecutive_failures_and_leaves_the_run_open(
    session, session_factory, tmdb_client, run_id
):
    """A TMDB outage stops the sweep rather than burning 7,519 requests on it. The run
    stays `running`: one row covers both phases, so the terminal status is the
    entrypoint's to write (NEU-1079)."""
    film = await _seed_director(session)
    for person_id in (11, 12, 13):
        await add_credit(
            session, film, person_id, credit_type="cast", department="Acting", credit_order=0
        )
    await session.commit()
    for person_id in (10, 11, 12):
        respx.get(f"{BASE_URL}/person/{person_id}/movie_credits").mock(
            return_value=httpx.Response(500)
        )
    last = respx.get(f"{BASE_URL}/person/13/movie_credits")

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=3)

    assert result.aborted is True
    assert result.abort_error is not None
    assert not last.called, "the sweep must stop at the threshold, not merely report it"
    assert (await _run_row(session, run_id)).status == "running"


@respx.mock
async def test_a_success_resets_the_consecutive_failure_count(
    session, session_factory, tmdb_client, run_id
):
    film = await _seed_director(session)
    for person_id in (11, 12):
        await add_credit(
            session, film, person_id, credit_type="cast", department="Acting", credit_order=0
        )
    await session.commit()
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(500))
    _mock_credits(11)
    respx.get(f"{BASE_URL}/person/12/movie_credits").mock(return_value=httpx.Response(500))

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=2)

    assert result.aborted is False
    assert result.person_failures == 2


@respx.mock
async def test_survives_a_malformed_credits_payload(session, session_factory, tmdb_client, run_id):
    # A 45-minute sweep must not die on one entry TMDB sent without a title.
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(
        return_value=httpx.Response(200, json={"id": 10, "crew": [{"id": 100, "job": "Director"}]})
    )
    _mock_credits(11, cast=[make_credit_entry(101, order=0)])
    _mock_details(101)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.person_failures == 1
    assert result.candidates_found == 1


@respx.mock
async def test_a_candidate_below_the_corroboration_threshold_is_not_admitted(
    session, session_factory, tmdb_client, run_id
):
    """One attachment is the earliest signal the product sells and also what a speculative
    TMDB entry looks like (§4.2), so the bar is a configured constant rather than a
    judgement made in code."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100)

    result = await _run(
        session_factory,
        tmdb_client,
        run_id,
        tranches=DIRECTORS_ONLY,
        corroboration_threshold=2,
    )

    assert (result.admitted, result.skipped_below_corroboration) == (0, 1)
    assert result.withheld == 0, "withheld means an open tranche was missing, not a low count"
    assert await _film_count(session) == 1


@respx.mock
async def test_a_candidate_meeting_the_threshold_is_admitted(
    session, session_factory, tmdb_client, run_id
):
    """Two distinct seed people reach the film, one of them at an open grade."""
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_credits(11, cast=[make_credit_entry(100, order=0)])
    _mock_details(100)

    result = await _run(
        session_factory,
        tmdb_client,
        run_id,
        tranches=DIRECTORS_ONLY,
        corroboration_threshold=2,
    )

    assert (result.admitted, result.skipped_below_corroboration) == (1, 0)


@respx.mock
async def test_the_attachment_histogram_still_counts_what_the_threshold_excluded(
    session, session_factory, tmdb_client, run_id
):
    """The histogram is what the M4 tuning ticket reads the threshold *off* (§4.3). Letting
    the current threshold truncate it would leave the next tuning pass unable to see that it
    should be lowered."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100)

    result = await _run(
        session_factory,
        tmdb_client,
        run_id,
        tranches=DIRECTORS_ONLY,
        corroboration_threshold=2,
    )

    assert result.attachment_histogram == {1: 1}
    # Asserted through the detail line too: the histogram is durable only if it survives
    # into `ingest_run.detail`, and that is the surface the tuning pass actually reads.
    assert "seed attachments: 1×1" in sweep_detail(
        result, RefreshResult(), FieldEventResult(), CreditEventResult(), ReleaseEventResult()
    )


@respx.mock
async def test_a_candidate_reached_only_through_a_writer_waits_for_its_tranche(
    session, session_factory, tmdb_client, run_id
):
    """Directors open, writers still closed: the ramp is per seed grade, and the role that
    matters is the one held *on the candidate film* (§4.1, §7.4)."""
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 11, credit_type="crew", department="Writing", job="Writer")
    await session.commit()
    _mock_credits(11, crew=[make_credit_entry(100, department="Writing", job="Writer")])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert (result.admitted, result.withheld) == (0, 1)
    assert await _film_count(session) == 1
    # A closed tranche withholds the film but must not hide it from the distribution: this
    # line is the answer to what opening the writers tranche would admit (NEU-1089).
    assert "seed attachments: 1×1" in sweep_detail(
        result, RefreshResult(), FieldEventResult(), CreditEventResult(), ReleaseEventResult()
    )


@pytest.mark.parametrize("job", ["Writer", "Screenplay"])
@respx.mock
async def test_the_writers_tranche_admits_a_candidate_reached_only_through_a_writer(
    session, session_factory, tmdb_client, run_id, job
):
    """The other half of the step above — same candidate, same seed person, and the flag is
    the only thing that changed (NEU-1089). Both writing jobs are the same seed grade
    (§3.2), so `Screenplay` starts admitting films for the first time with this tranche.
    """
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 11, credit_type="crew", department="Writing", job=job)
    await session.commit()
    _mock_credits(11, crew=[make_credit_entry(100, department="Writing", job=job)])
    _mock_details(100, title="Untitled Script")

    result = await _run(session_factory, tmdb_client, run_id, tranches=WRITERS_ONLY)

    assert (result.admitted, result.withheld) == (1, 0)
    admitted = (await session.execute(select(Film).where(Film.tmdb_id == 100))).scalar_one()
    assert admitted.title == "Untitled Script"


@pytest.mark.parametrize(
    ("tranches", "expected"), [(DIRECTORS_ONLY, (0, 1)), (WRITERS_ONLY, (1, 0))]
)
@respx.mock
async def test_a_director_seed_who_only_wrote_the_candidate_is_a_writers_admission(
    session, session_factory, tmdb_client, run_id, tranches, expected
):
    """The grade that admits is the one held *on the candidate film*, not the one that made
    the person a seed (§4.1 rule 2) — a director whose next project is one they only wrote
    is a writers admission. Were it read off the person instead, the directors tranche would
    have been admitting writer-reached films all along and the ramp would measure nothing.
    """
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Writing", job="Writer")])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id, tranches=tranches)

    assert (result.admitted, result.withheld) == expected


@pytest.mark.parametrize(
    ("tranches", "admitted_ids", "counts"),
    [(DIRECTORS_ONLY, [100], (1, 1)), (DIRECTORS_AND_WRITERS, [100, 101], (2, 0))],
)
@respx.mock
async def test_opening_writers_admits_the_writer_reached_film_and_nothing_else_changes(
    session, session_factory, tmdb_client, run_id, tranches, admitted_ids, counts
):
    """The ramp only measures anything if each step is additive (§7.4): opening writers adds
    the film a writer reached and leaves the director-reached one exactly as it was, so a
    precision drop after the flip names the grade that caused it."""
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="crew", department="Writing", job="Writer")
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_credits(11, crew=[make_credit_entry(101, department="Writing", job="Writer")])
    _mock_details(100)
    _mock_details(101)

    result = await _run(session_factory, tmdb_client, run_id, tranches=tranches)

    admitted = (
        (
            await session.execute(
                select(Film.tmdb_id).where(Film.tmdb_id.in_([100, 101])).order_by(Film.tmdb_id)
            )
        )
        .scalars()
        .all()
    )
    assert admitted == admitted_ids
    assert (result.admitted, result.withheld) == counts


@respx.mock
async def test_the_cast_tranche_admits_a_candidate_reached_at_the_fifth_billing(
    session, session_factory, tmdb_client, run_id
):
    """`order` is 0-indexed, so 4 is the fifth billing — the last one inside the cut, and
    the boundary the whole grade turns on (§3.2, NEU-1090)."""
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=4)
    await session.commit()
    _mock_credits(11, cast=[make_credit_entry(100, order=4)])
    _mock_details(100, title="Untitled Ensemble Picture")

    result = await _run(session_factory, tmdb_client, run_id, tranches=CAST_ONLY)

    assert (result.admitted, result.withheld) == (1, 0)
    admitted = (await session.execute(select(Film).where(Film.tmdb_id == 100))).scalar_one()
    assert admitted.title == "Untitled Ensemble Picture"


@respx.mock
async def test_a_sixth_billed_credit_is_never_reached_rather_than_withheld(
    session, session_factory, tmdb_client, run_id
):
    """The other half of the boundary, and it fails *earlier* than a closed tranche does: a
    sixth-billed credit is not seed grade at all, so the film is never a candidate. That
    distinction is the one the run's detail line reports — `no_tranche` says "an operator
    could open this", and a film outside the billing cut is not that. Opening every tranche
    must not reach it either, which is what separates the cut from the ramp."""
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    _mock_credits(11, cast=[make_credit_entry(100, order=5)])
    details = respx.get(f"{BASE_URL}/movie/100")

    result = await _run(
        session_factory,
        tmdb_client,
        run_id,
        tranches=AdmissionTranches(enabled=True, directors=True, writers=True, cast=True),
    )

    assert (result.candidates_found, result.admitted, result.withheld) == (0, 0, 0)
    assert not details.called, "an unreached candidate must not cost a details fetch"
    assert await _film_count(session) == 1


@respx.mock
async def test_the_master_flag_off_admits_nothing_however_the_tranches_are_set(
    session, session_factory, tmdb_client, run_id
):
    """`SWEEP_ENABLED` is the rollback, kept separate from the ramp on purpose (§7.3): one
    move has to stop admission without anyone having to remember which tranches were open."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100)

    result = await _run(
        session_factory,
        tmdb_client,
        run_id,
        tranches=AdmissionTranches(enabled=False, directors=True, writers=True, cast=True),
    )

    assert (result.admitted, result.withheld) == (0, 1)
    assert await _film_count(session) == 1


@respx.mock
async def test_admission_lands_the_alternative_titles_the_retrieval_index_needs(
    session, session_factory, tmdb_client, run_id
):
    """Admission is a full `movie_details` fetch, not a stub row: the link stage's candidate
    retrieval matches on alternative titles, and credits are what make the admitted film
    contribute its own seed people back on the next sweep (§3.3)."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    respx.get(f"{BASE_URL}/movie/100").mock(
        return_value=httpx.Response(
            200,
            json=make_details(
                100,
                release_date="",
                status="Planned",
                alternative_titles={
                    "titles": [{"iso_3166_1": "FR", "title": "Projet Sans Titre", "type": ""}]
                },
                credits={
                    "cast": [],
                    "crew": [
                        {
                            "id": 10,
                            "name": "Person 10",
                            "credit_id": "c-100-10",
                            "department": "Directing",
                            "job": "Director",
                        }
                    ],
                },
            ),
        )
    )

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.admitted == 1
    admitted = (await session.execute(select(Film).where(Film.tmdb_id == 100))).scalar_one()
    titles = (
        (
            await session.execute(
                select(FilmAlternativeTitle.title).where(
                    FilmAlternativeTitle.film_id == admitted.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert titles == ["Projet Sans Titre"]
    jobs = (
        (await session.execute(select(FilmCredit.job).where(FilmCredit.film_id == admitted.id)))
        .scalars()
        .all()
    )
    assert jobs == ["Director"]


@respx.mock
async def test_a_closed_tranche_is_reported_ahead_of_a_low_attachment_count(
    session, session_factory, tmdb_client, run_id
):
    """A candidate can fail both gates, and the run's `detail` line reports one reason per
    candidate — so which one it names matters. The closed tranche is the operator's own
    setting and the threshold was never consulted, so reporting `corroboration` here would
    read as a verdict on the film that the sweep never actually reached."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, department="Directing", job="Director")])
    _mock_details(100)

    result = await _run(
        session_factory,
        tmdb_client,
        run_id,
        tranches=AdmissionTranches(),
        corroboration_threshold=2,
    )

    assert (result.withheld, result.skipped_below_corroboration) == (1, 0)
    assert result.skip_counts == {"no_tranche": 1}


# --- A person or candidate TMDB has deleted is terminal, not an outage (NEU-1124) ---


@respx.mock
async def test_a_404_person_is_missing_not_failed(session, session_factory, tmdb_client, run_id):
    """~50 of these fire on every production run. Reported as failures they read as a flaky
    TMDB, and they sit one unlucky ordering away from aborting the phase outright."""
    film = await _seed_director(session)
    await add_credit(session, film, 11, credit_type="cast", department="Acting", credit_order=0)
    await session.commit()
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(404))
    _mock_credits(11, cast=[make_credit_entry(100, order=0)])
    _mock_details(100)

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.person_missing, result.person_failures) == (1, 0)
    assert result.candidates_found == 1, "the other person's filmography is still read"
    assert (await _run_row(session, run_id)).items_failed == 0
    person = await session.get(Person, 10, populate_existing=True)
    assert person.tmdb_missing_at is not None, "the dead seed person is tombstoned"


@respx.mock
async def test_a_tombstoned_person_leaves_the_seed_set(
    session, session_factory, tmdb_client, run_id
):
    """The half that stops the daily cost. Their credit rows are ours and outlive the person
    record upstream, so without the tombstone the dead filmography is requested every run."""
    await _seed_director(session)
    await session.commit()
    route = respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(404))

    first = await _run(session_factory, tmdb_client, run_id)
    assert first.seed_people == 1

    second = await _run(session_factory, tmdb_client, run_id)

    assert second.seed_people == 0, "a tombstoned person is no longer a seed"
    assert route.call_count == 1, "a dead person id costs one request, not one per run"


@respx.mock
async def test_a_person_tmdb_names_again_returns_to_the_seed_set(
    session, session_factory, tmdb_client, run_id
):
    """No cadence needed on this side: the person upsert that runs on every film ingest is
    what clears the tombstone, so a restored person comes back on their own."""
    film = await _seed_director(session)
    await session.commit()
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(404))
    await _run(session_factory, tmdb_client, run_id)
    assert (await _run(session_factory, tmdb_client, run_id)).seed_people == 0

    # TMDB names them in a film's credits again — the ordinary ingest path.
    async with session_factory() as s:
        await upsert_film(
            s,
            TMDBMovieDetails.model_validate(
                make_details(
                    film.tmdb_id,
                    credits={
                        "cast": [],
                        "crew": [
                            {
                                "id": 10,
                                "name": "Person 10",
                                "job": "Director",
                                "department": "Directing",
                                "credit_id": "c-restored",
                            }
                        ],
                    },
                )
            ),
        )
        await s.commit()

    person = await session.get(Person, 10, populate_existing=True)
    assert person.tmdb_missing_at is None, "the upsert cleared the tombstone"


@respx.mock
async def test_a_whole_seed_set_of_404s_does_not_abort_the_phase(
    session, session_factory, tmdb_client, run_id
):
    film = await _seed_director(session)
    for person_id in range(20, 35):
        await add_credit(
            session, film, person_id, credit_type="cast", department="Acting", credit_order=0
        )
        respx.get(f"{BASE_URL}/person/{person_id}/movie_credits").mock(
            return_value=httpx.Response(404)
        )
    respx.get(f"{BASE_URL}/person/10/movie_credits").mock(return_value=httpx.Response(404))
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=10)

    assert not result.aborted
    assert result.person_missing == 16
    assert result.person_failures == 0


@respx.mock
async def test_a_404_candidate_is_skipped_as_missing(session, session_factory, tmdb_client, run_id):
    """Deleted between the credits read and the details fetch. Nothing is tombstoned — the
    film was never admitted, so there is no catalog row to mark."""
    await _seed_director(session)
    await session.commit()
    _mock_credits(10, crew=[make_credit_entry(100, job="Director")])
    respx.get(f"{BASE_URL}/movie/100").mock(return_value=httpx.Response(404))

    result = await _run(session_factory, tmdb_client, run_id, tranches=DIRECTORS_ONLY)

    assert result.skipped_missing == 1
    assert result.candidate_failures == 0
    assert not result.aborted
    assert result.skip_counts["missing"] == 1
