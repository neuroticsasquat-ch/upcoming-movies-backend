from datetime import UTC, date, datetime

from sqlalchemy import func, select, text

from tests.fixtures.tmdb import make_details
from upmovies.catalog.models import (
    Collection,
    Film,
    FilmGenre,
    FilmProductionCountry,
    FilmReleaseDate,
)
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


# ---------------------------------------------------------------------------
# FilmReleaseDate model tests (Task 1 — NEU-404)
# ---------------------------------------------------------------------------


async def _make_film(session) -> Film:
    """Insert a minimal Film row and return it (needed to satisfy the FK)."""
    details = TMDBMovieDetails.model_validate(make_details(99999, title="Test Film"))
    await upsert_film(session, details)
    await session.commit()
    result = await session.execute(
        select(Film).where(Film.tmdb_id == 99999),
        execution_options={"populate_existing": True},
    )
    return result.scalar_one()


async def test_film_release_date_insert_and_read(session):
    """FilmReleaseDate row can be inserted and queried with all columns correct."""
    film = await _make_film(session)
    release_dt = datetime(2026, 7, 16, 0, 0, 0, tzinfo=UTC)

    row = FilmReleaseDate(
        film_id=film.id,
        iso_3166_1="US",
        release_type=3,
        release_date=release_dt,
        certification="PG-13",
        note="wide release",
        iso_639_1="en",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.id is not None
    assert row.film_id == film.id
    assert row.iso_3166_1 == "US"
    assert row.release_type == 3
    assert row.release_date == release_dt
    assert row.certification == "PG-13"
    assert row.note == "wide release"
    assert row.iso_639_1 == "en"


async def test_film_release_date_nullable_columns(session):
    """FilmReleaseDate nullable columns (certification, note, iso_639_1) accept None."""
    film = await _make_film(session)
    release_dt = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)

    row = FilmReleaseDate(
        film_id=film.id,
        iso_3166_1="GB",
        release_type=1,
        release_date=release_dt,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    assert row.certification is None
    assert row.note is None
    assert row.iso_639_1 is None


async def test_film_release_date_lookup_index_exists(session):
    """ix_catalog_film_release_date_lookup index is present in pg_indexes."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'film_release_date' AND schemaname = 'catalog'"
        )
    )
    index_names = {row[0] for row in result}
    assert "ix_catalog_film_release_date_lookup" in index_names


# ---------------------------------------------------------------------------
# upsert_film → FilmReleaseDate rebuild tests (Task 3 — NEU-404)
# ---------------------------------------------------------------------------

_RELEASE_DATES_2_COUNTRIES = {
    "results": [
        {
            "iso_3166_1": "US",
            "release_dates": [
                {
                    "certification": "PG-13",
                    "iso_639_1": "en",
                    "note": "",
                    "release_date": "2026-07-16T00:00:00.000Z",
                    "type": 3,
                },
                {
                    "certification": "PG-13",
                    "iso_639_1": "en",
                    "note": "Netflix",
                    "release_date": "2026-07-23T00:00:00.000Z",
                    "type": 4,
                },
            ],
        },
        {
            "iso_3166_1": "GB",
            "release_dates": [
                {
                    "certification": "12A",
                    "iso_639_1": "en",
                    "note": "",
                    "release_date": "2026-07-18T00:00:00.000Z",
                    "type": 3,
                },
                {
                    "certification": "12A",
                    "iso_639_1": "en",
                    "note": "Disney+",
                    "release_date": "2026-07-25T00:00:00.000Z",
                    "type": 4,
                },
            ],
        },
    ]
}

_RELEASE_DATES_CHANGED = {
    "results": [
        {
            "iso_3166_1": "FR",
            "release_dates": [
                {
                    "certification": "U",
                    "iso_639_1": "fr",
                    "note": "",
                    "release_date": "2026-08-01T00:00:00.000Z",
                    "type": 3,
                },
            ],
        },
    ]
}


async def _release_dates(session, film_id) -> list[FilmReleaseDate]:
    result = await session.execute(
        select(FilmReleaseDate).where(FilmReleaseDate.film_id == film_id),
        execution_options={"populate_existing": True},
    )
    return list(result.scalars().all())


async def test_upsert_populates_release_date_rows(session):
    """upsert_film with a release_dates block inserts one FilmReleaseDate row per entry."""
    details = TMDBMovieDetails.model_validate(
        make_details(101, release_dates=_RELEASE_DATES_2_COUNTRIES)
    )
    await upsert_film(session, details)
    await session.commit()

    film = (await _films(session))[0]
    rows = await _release_dates(session, film.id)
    assert len(rows) == 4

    # Spot-check one row from each country
    us_rows = sorted([r for r in rows if r.iso_3166_1 == "US"], key=lambda r: r.release_type)
    gb_rows = sorted([r for r in rows if r.iso_3166_1 == "GB"], key=lambda r: r.release_type)
    assert len(us_rows) == 2
    assert len(gb_rows) == 2

    assert us_rows[0].release_type == 3
    assert us_rows[0].certification == "PG-13"
    assert us_rows[1].release_type == 4
    assert us_rows[1].note == "Netflix"
    assert gb_rows[0].certification == "12A"


async def test_upsert_release_dates_idempotent(session):
    """Re-running upsert_film with same release_dates data produces no duplicate rows."""
    details = TMDBMovieDetails.model_validate(
        make_details(102, release_dates=_RELEASE_DATES_2_COUNTRIES)
    )
    await upsert_film(session, details)
    await session.commit()
    await upsert_film(session, details)
    await session.commit()

    film = (await _films(session))[0]
    rows = await _release_dates(session, film.id)
    assert len(rows) == 4


async def test_upsert_release_dates_rebuild_on_change(session):
    """Re-upserting with different release_dates replaces stale rows with the new set."""
    details_v1 = TMDBMovieDetails.model_validate(
        make_details(103, release_dates=_RELEASE_DATES_2_COUNTRIES)
    )
    await upsert_film(session, details_v1)
    await session.commit()

    film = (await _films(session))[0]
    rows_v1 = await _release_dates(session, film.id)
    assert len(rows_v1) == 4

    details_v2 = TMDBMovieDetails.model_validate(
        make_details(103, release_dates=_RELEASE_DATES_CHANGED)
    )
    await upsert_film(session, details_v2)
    await session.commit()

    rows_v2 = await _release_dates(session, film.id)
    assert len(rows_v2) == 1
    assert rows_v2[0].iso_3166_1 == "FR"
    assert rows_v2[0].certification == "U"


async def test_upsert_release_dates_cleared_on_empty_payload(session):
    """A film whose later ingest omits the release_dates block has its stale rows cleared,
    matching the delete-then-reinsert rebuild contract used for the other join relations."""
    details_v1 = TMDBMovieDetails.model_validate(
        make_details(107, release_dates=_RELEASE_DATES_2_COUNTRIES)
    )
    await upsert_film(session, details_v1)
    await session.commit()

    film = (await _films(session))[0]
    assert len(await _release_dates(session, film.id)) == 4

    # Re-ingest the same film with no release_dates block at all.
    details_v2 = TMDBMovieDetails.model_validate(make_details(107))
    await upsert_film(session, details_v2)
    await session.commit()

    assert await _release_dates(session, film.id) == []


async def test_upsert_no_release_dates_is_noop(session):
    """upsert_film without a release_dates key leaves film_release_date empty (no error)."""
    details = TMDBMovieDetails.model_validate(make_details(104))
    await upsert_film(session, details)
    await session.commit()

    film = (await _films(session))[0]
    rows = await _release_dates(session, film.id)
    assert rows == []


async def test_upsert_skips_release_date_entries_with_empty_date(session):
    """upsert_film with a mix of valid and empty-date entries inserts only the valid rows."""
    release_dates_with_empty = {
        "results": [
            {
                "iso_3166_1": "US",
                "release_dates": [
                    {
                        "certification": "NR",
                        "iso_639_1": "en",
                        "note": "",
                        "release_date": "",  # TMDB empty-string unknown date
                        "type": 3,
                    },
                    {
                        "certification": "PG-13",
                        "iso_639_1": "en",
                        "note": "wide",
                        "release_date": "2026-07-16T00:00:00.000Z",
                        "type": 4,
                    },
                ],
            }
        ]
    }
    details = TMDBMovieDetails.model_validate(
        make_details(200, release_dates=release_dates_with_empty)
    )
    # No exception should be raised
    await upsert_film(session, details)
    await session.commit()

    film = (await _films(session))[0]
    rows = await _release_dates(session, film.id)
    # Only the entry with a real date is persisted
    assert len(rows) == 1
    assert rows[0].release_type == 4
    assert rows[0].certification == "PG-13"
    assert rows[0].release_date is not None


async def test_upsert_release_dates_cascade_delete(session):
    """Deleting the Film row cascade-deletes its FilmReleaseDate rows."""
    from sqlalchemy import delete as sa_delete

    details = TMDBMovieDetails.model_validate(
        make_details(105, release_dates=_RELEASE_DATES_2_COUNTRIES)
    )
    await upsert_film(session, details)
    await session.commit()

    film = (await _films(session))[0]
    rows_before = await _release_dates(session, film.id)
    assert len(rows_before) == 4

    await session.execute(sa_delete(Film).where(Film.id == film.id))
    await session.commit()

    rows_after = await _release_dates(session, film.id)
    assert rows_after == []
