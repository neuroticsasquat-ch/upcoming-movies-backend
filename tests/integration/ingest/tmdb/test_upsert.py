from datetime import date

from sqlalchemy import func, select

from tests.fixtures.tmdb import make_details
from upmovies.catalog.models import Collection, Film, FilmGenre, FilmProductionCountry
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


async def _genre_ids(session, film_id) -> set[int]:
    rows = await session.execute(select(FilmGenre.genre_id).where(FilmGenre.film_id == film_id))
    return set(rows.scalars().all())


async def test_upsert_persists_scalars_and_raw(session):
    details = TMDBMovieDetails.model_validate(make_details(11, title="Star Wars"))
    details.tmdb_raw = {"id": 11, "unmodeled": "kept"}
    await upsert_film(session, details)
    await session.commit()

    film = (await _films(session))[0]
    assert film.budget == 1_000_000
    assert film.runtime == 120
    assert film.origin_country == ["US"]
    assert film.vote_count == 100
    assert film.tmdb_raw == {"id": 11, "unmodeled": "kept"}


async def test_upsert_creates_reference_and_join_rows(session):
    details = TMDBMovieDetails.model_validate(make_details(11))
    await upsert_film(session, details)
    await session.commit()

    film = (await _films(session))[0]
    assert await _genre_ids(session, film.id) == {28, 12}
    countries = await session.execute(
        select(FilmProductionCountry.iso_3166_1).where(FilmProductionCountry.film_id == film.id)
    )
    assert set(countries.scalars().all()) == {"US"}


async def test_upsert_sets_collection_fk_and_upserts_collection(session):
    details = TMDBMovieDetails.model_validate(
        make_details(
            11,
            belongs_to_collection={
                "id": 10,
                "name": "Star Wars Collection",
                "poster_path": "/p.jpg",
                "backdrop_path": "/b.jpg",
            },
        )
    )
    await upsert_film(session, details)
    await session.commit()

    film = (await _films(session))[0]
    assert film.collection_id == 10
    coll = (await session.execute(select(Collection).where(Collection.id == 10))).scalar_one()
    assert coll.name == "Star Wars Collection"


async def test_upsert_leaves_collection_null_when_absent(session):
    details = TMDBMovieDetails.model_validate(make_details(11, belongs_to_collection=None))
    await upsert_film(session, details)
    await session.commit()

    assert (await _films(session))[0].collection_id is None
    count = (await session.execute(select(func.count()).select_from(Collection))).scalar_one()
    assert count == 0


async def test_upsert_rebuilds_joins_on_change(session):
    await upsert_film(
        session,
        TMDBMovieDetails.model_validate(make_details(11, genres=[{"id": 28, "name": "Action"}])),
    )
    await session.commit()
    film = (await _films(session))[0]
    assert await _genre_ids(session, film.id) == {28}

    # Re-ingest with a different genre set: the stale join must be removed.
    await upsert_film(
        session,
        TMDBMovieDetails.model_validate(
            make_details(11, genres=[{"id": 878, "name": "Science Fiction"}])
        ),
    )
    await session.commit()
    assert await _genre_ids(session, film.id) == {878}


async def test_upsert_assigns_slug_on_insert(session):
    await upsert_film(
        session, TMDBMovieDetails.model_validate(make_details(27205, title="Inception"))
    )
    await session.commit()
    assert (await _films(session))[0].slug == "inception-2026"


async def test_upsert_preserves_slug_when_title_changes(session):
    await upsert_film(session, TMDBMovieDetails.model_validate(make_details(1, title="Old Title")))
    await session.commit()
    original_slug = (await _films(session))[0].slug
    assert original_slug == "old-title-2026"

    await upsert_film(
        session, TMDBMovieDetails.model_validate(make_details(1, title="Brand New Title"))
    )
    await session.commit()
    film = (await _films(session))[0]
    assert film.title == "Brand New Title"
    assert film.slug == original_slug, "slug is frozen on insert — never regenerated on update"


async def test_upsert_disambiguates_slug_collision_with_tmdb_id(session):
    # two distinct films with the same title + release year (defaults to 2026-07-15)
    await upsert_film(session, TMDBMovieDetails.model_validate(make_details(1, title="Dune")))
    await session.commit()
    await upsert_film(session, TMDBMovieDetails.model_validate(make_details(2, title="Dune")))
    await session.commit()

    films = await _films(session)  # ordered by tmdb_id ascending
    assert films[0].slug == "dune-2026"
    assert films[1].slug == "dune-2026-2"
