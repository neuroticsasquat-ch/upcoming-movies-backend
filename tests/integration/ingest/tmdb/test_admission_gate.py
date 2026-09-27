"""EF-4's admission exception applies to unreleased films only (NEU-1505, D-1505.1).

The 2026-09-27 digest carried `crew_attached` cards for four long-released David Fincher films,
because another user's import first-observed them and admission wrote every followed credit as
`added`. `upsert_film` now asks `is_unreleased` once and hands the answer to all three admission
call sites, so a released film's first observation is a baseline like any other.

The ordinary diff is untouched: the gate reads only the first-observation branch.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow
from upmovies.catalog.models import (
    COLLECTION_FIELD,
    Film,
    FilmCompanyChange,
    FilmCreditChange,
    FilmFieldChange,
)
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film

DIRECTOR = 7467
OTHER_DIRECTOR = 7468
STUDIO = 420
FRANCHISE = {"id": 726871, "name": "Dune Collection"}

TODAY = datetime.now(UTC).date()
RELEASED = {"release_date": (TODAY - timedelta(days=1)).isoformat(), "status": "Released"}
UNRELEASED = {
    "release_date": (TODAY + timedelta(days=365)).isoformat(),
    "status": "Post Production",
}


def _details(tmdb_id: int, dates: dict[str, str], *, director: int = DIRECTOR) -> TMDBMovieDetails:
    return TMDBMovieDetails.model_validate(
        make_details(
            tmdb_id,
            belongs_to_collection=FRANCHISE,
            production_companies=[{"id": STUDIO, "name": "Studio"}],
            credits={
                "cast": [],
                "crew": [
                    {
                        "id": director,
                        "name": f"Person {director}",
                        "credit_id": f"crew-{director}",
                        "department": "Directing",
                        "job": "Director",
                    }
                ],
            },
            release_date=dates["release_date"],
            status=dates["status"],
        )
    )


async def _follow(session, user, entity_type: str, entity_id: int) -> None:
    session.add(
        Follow(user_id=user.id, entity_type=entity_type, entity_id=str(entity_id), source="manual")
    )
    await session.commit()


async def _follow_all(session, make_user) -> None:
    user = await make_user(email="follower@example.com")
    await _follow(session, user, "person", DIRECTOR)
    await _follow(session, user, "company", STUDIO)
    await _follow(session, user, "franchise", FRANCHISE["id"])


async def _film(session, tmdb_id: int) -> Film:
    stmt = select(Film).where(Film.tmdb_id == tmdb_id)
    return (await session.execute(stmt, execution_options={"populate_existing": True})).scalar_one()


async def _changes(session, film: Film) -> tuple[list, list, list]:
    async def rows(model, *where):
        stmt = select(model).where(model.film_id == film.id, *where)
        return list((await session.execute(stmt)).scalars().all())

    return (
        await rows(FilmCreditChange),
        await rows(FilmCompanyChange),
        await rows(FilmFieldChange, FilmFieldChange.field == COLLECTION_FIELD),
    )


async def test_a_released_films_first_observation_writes_no_attachment(session, make_user):
    await _follow_all(session, make_user)

    await upsert_film(session, _details(9300, RELEASED))
    await session.commit()

    film = await _film(session, 9300)
    assert await _changes(session, film) == ([], [], [])
    # Still observed: the next ingest diffs against this baseline rather than admitting again.
    assert film.credits_observed_at is not None
    assert film.companies_observed_at is not None


async def test_an_unreleased_films_first_observation_still_writes_all_three(session, make_user):
    await _follow_all(session, make_user)

    await upsert_film(session, _details(9301, UNRELEASED))
    await session.commit()

    credits, companies, collections = await _changes(session, await _film(session, 9301))
    assert [(c.person_id, c.change) for c in credits] == [(DIRECTOR, "added")]
    assert [(c.company_id, c.change) for c in companies] == [(STUDIO, "added")]
    assert [(c.old_value, c.new_value) for c in collections] == [(None, FRANCHISE["id"])]


async def test_a_follow_after_a_released_films_baseline_writes_nothing(session, make_user):
    # D-49's property, unchanged by the gate: observed once with nobody following, then a
    # follow, then an identical second ingest — nothing moved, so nothing is written.
    await upsert_film(session, _details(9302, RELEASED))
    await session.commit()
    await _follow_all(session, make_user)

    await upsert_film(session, _details(9302, RELEASED))
    await session.commit()

    assert await _changes(session, await _film(session, 9302)) == ([], [], [])


async def test_a_released_film_already_observed_still_diffs(session, make_user):
    # The gate touches the first-observation branch only: a director who genuinely attaches to
    # a released film between two ingests is an ordinary `added` row.
    await upsert_film(session, _details(9303, RELEASED, director=OTHER_DIRECTOR))
    await session.commit()
    await _follow_all(session, make_user)

    await upsert_film(session, _details(9303, RELEASED))
    await session.commit()

    credits, _, _ = await _changes(session, await _film(session, 9303))
    assert (DIRECTOR, "added") in {(c.person_id, c.change) for c in credits}
