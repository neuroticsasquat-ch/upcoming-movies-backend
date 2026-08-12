"""The sweep's two catalog reads: the seed people, and the films we already hold. Moved
from the probe's suite (NEU-1073) along with the queries themselves.

One test here also calls `seed_attachments`, which needs no database — it is the only place
the SQL-side and Python-side halves of the top-billed cut can be asserted to agree, and that
agreement is the point of it."""

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fixtures.catalog import add_credit, add_film
from tests.fixtures.tmdb import make_credit_entry, make_person_movie_credits
from upmovies.catalog.models import FilmFieldChange
from upmovies.ingest.sweep import load_known_film_tmdb_ids, load_seed_person_ids, seed_attachments
from upmovies.ingest.tmdb.schemas import TMDBPersonMovieCredits

EXCLUDED = frozenset({"Released", "Canceled"})
TODAY = date(2026, 8, 10)
DORMANCY_DAYS = 365
DATED = TODAY + timedelta(days=90)


async def _seed_ids(session) -> list[int]:
    return await load_seed_person_ids(
        session, today=TODAY, excluded_statuses=EXCLUDED, dormancy_days=DORMANCY_DAYS
    )


async def test_seed_people_are_directors_writers_and_top_five_cast(session):
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 10, credit_type="crew", department="Directing", job="Director")
    await add_credit(session, film, 11, credit_type="crew", department="Writing", job="Writer")
    await add_credit(session, film, 12, credit_type="crew", department="Writing", job="Screenplay")
    await add_credit(session, film, 13, credit_type="cast", department="Acting", credit_order=4)

    assert await _seed_ids(session) == [10, 11, 12, 13]


@pytest.mark.parametrize(
    ("credit_order", "seed_grade"), [(0, True), (4, True), (5, False), (None, False)]
)
async def test_the_top_billed_cut_agrees_on_both_sides_of_the_sweep(
    session, credit_order, seed_grade
):
    """`credit_order < TOP_BILLED_ORDER` is expressed **twice**: inlined into
    `load_seed_person_ids`'s SQL, which decides whose filmography is worth a request, and
    via `is_top_billed` in `seed_attachments`, which decides whether their role on a
    candidate film can admit it (§3.2). The existing tests pin each side on its own; this
    one pins them to each other, at the boundary and off both ends of it.

    Two encodings of one cut can drift, and the cast tranche is what makes that expensive
    — it is the grade the two would disagree about, and a person the SQL drops is one whose
    films are never reached at *any* grade, silently (NEU-1090).

    The billing positions are **literals, not `TOP_BILLED_ORDER` arithmetic**: expressing
    them in terms of the constant would make the test agree with whatever the constant says,
    and the measurement in `catalog/seed_grade.py` is specifically that 5 is the right value
    and tightening to 3 is backwards. This has to fail if someone moves it.
    """
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(
        session, film, 30, credit_type="cast", department="Acting", credit_order=credit_order
    )
    credits = TMDBPersonMovieCredits.model_validate(
        make_person_movie_credits(30, cast=[make_credit_entry(100, order=credit_order)])
    )

    assert await _seed_ids(session) == ([30] if seed_grade else [])
    assert bool(seed_attachments(30, credits)) is seed_grade


async def test_weaker_credits_are_not_seed_grade(session):
    """Producers are excluded by decision, not by omission: an EP credit travels far and
    says little about whether a project is real (§3.2)."""
    film = await add_film(session, 1, release_date=DATED)
    await add_credit(session, film, 20, credit_type="cast", department="Acting", credit_order=5)
    await add_credit(session, film, 21, credit_type="crew", department="Production", job="Producer")
    await add_credit(
        session, film, 22, credit_type="crew", department="Sound", job="Original Music Composer"
    )

    assert await _seed_ids(session) == []


async def test_seed_people_derive_only_from_active_films(session):
    released = await add_film(session, 1, release_date=TODAY - timedelta(days=1), status="Released")
    canceled = await add_film(session, 2, release_date=None, status="Canceled")
    undated = await add_film(session, 3, release_date=None, status="Planned")
    await add_credit(session, released, 30, credit_type="crew", job="Director")
    await add_credit(session, canceled, 31, credit_type="crew", job="Director")
    await add_credit(session, undated, 32, credit_type="crew", job="Director")

    assert await _seed_ids(session) == [32]


async def test_dormant_films_contribute_no_seed_people_until_revived(session):
    """The seed set is the third cost curve dormancy governs (ADR-0015), and it revives
    the same way: one field change and the person is back."""
    dormant = await add_film(
        session,
        1,
        release_date=None,
        status="Planned",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    await add_credit(session, dormant, 50, credit_type="crew", job="Director")
    await session.commit()

    assert await _seed_ids(session) == []

    session.add(
        FilmFieldChange(
            film_id=dormant.id,
            field="overview",
            new_value="x",
            changed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    await session.commit()

    assert await _seed_ids(session) == [50]


async def test_a_person_seeded_by_several_films_is_enumerated_once(session):
    for tmdb_id in (1, 2):
        film = await add_film(session, tmdb_id, release_date=DATED)
        await add_credit(session, film, 40, credit_type="crew", job="Director")

    assert await _seed_ids(session) == [40]


async def test_known_film_ids_include_inactive_films(session):
    """A film we already hold is not a candidate whatever state it is in — otherwise the
    sweep re-admits everything that ever went dormant, every day."""
    await add_film(session, 1, release_date=DATED)
    await add_film(session, 2, release_date=TODAY - timedelta(days=400), status="Released")

    assert await load_known_film_tmdb_ids(session) == {1, 2}
