from datetime import date

from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.catalog.models import Film
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film


async def _films(session) -> list[Film]:
    # populate_existing: our upserts run as Core statements, so force the ORM identity
    # map to refresh from the DB rather than returning stale cached instances.
    result = await session.execute(
        select(Film).order_by(Film.tmdb_id),
        execution_options={"populate_existing": True},
    )
    return list(result.scalars().all())


async def test_upsert_inserts_new_film(session):
    details = TMDBMovieDetails.model_validate(make_details(27205, title="Inception"))
    await upsert_film(session, details)
    await session.commit()

    films = await _films(session)
    assert len(films) == 1
    film = films[0]
    assert film.tmdb_id == 27205
    assert film.title == "Inception"
    assert film.imdb_id == "tt0027205"
    assert film.status == "Released"
    assert film.release_date == date(2026, 7, 15)


async def test_upsert_updates_existing_film_by_tmdb_id(session):
    await upsert_film(session, TMDBMovieDetails.model_validate(make_details(1, title="Old")))
    await session.commit()
    original = (await _films(session))[0]
    original_id = original.id
    original_created = original.created_at

    await upsert_film(
        session,
        TMDBMovieDetails.model_validate(
            make_details(1, title="New", status="Post Production", overview="changed")
        ),
    )
    await session.commit()

    films = await _films(session)
    assert len(films) == 1, "update must not create a second row"
    film = films[0]
    assert film.id == original_id, "stable surrogate key preserved across update"
    assert film.created_at == original_created
    assert film.title == "New"
    assert film.status == "Post Production"
    assert film.overview == "changed"
    assert film.updated_at >= original.updated_at


async def test_upsert_is_idempotent(session):
    details = TMDBMovieDetails.model_validate(make_details(42))
    await upsert_film(session, details)
    await session.commit()
    await upsert_film(session, details)
    await session.commit()

    films = await _films(session)
    assert len(films) == 1
    assert films[0].tmdb_id == 42
