"""The sweep's two catalog reads: the seed people, and the films we already hold. Moved
from the probe's suite (NEU-1073) along with the queries themselves."""

from datetime import UTC, date, datetime, timedelta

from tests.fixtures.catalog import add_credit, add_film
from upmovies.catalog.models import FilmFieldChange
from upmovies.ingest.sweep import load_known_film_tmdb_ids, load_seed_person_ids

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
