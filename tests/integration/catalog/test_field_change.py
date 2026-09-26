from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film, FilmFieldChange
from upmovies.catalog.queries import field_changed_at


async def test_field_changed_at_returns_latest_row(session):
    film = Film(tmdb_id=990001, title="A")
    session.add(film)
    await session.flush()

    t_old = datetime(2026, 1, 1, tzinfo=UTC)
    t_new = datetime(2026, 6, 1, tzinfo=UTC)
    session.add_all(
        [
            FilmFieldChange(
                film_id=film.id,
                field="release_date",
                old_value=None,
                new_value="2027-01-01",
                changed_at=t_old,
            ),
            FilmFieldChange(
                film_id=film.id,
                field="release_date",
                old_value="2027-01-01",
                new_value="2027-06-30",
                changed_at=t_new,
            ),
            FilmFieldChange(
                film_id=film.id,
                field="title",
                old_value="A",
                new_value="B",
                changed_at=t_new,
            ),
        ]
    )
    await session.flush()

    assert await field_changed_at(session, film.id, "release_date") == t_new


async def test_field_changed_at_none_when_absent(session):
    film = Film(tmdb_id=990002, title="A")
    session.add(film)
    await session.flush()

    assert await field_changed_at(session, film.id, "release_date") is None


async def test_tracked_column_update_logs_one_row(session):
    film = Film(tmdb_id=990010, title="Original")
    session.add(film)
    await session.flush()  # INSERT does not fire a BEFORE UPDATE trigger

    film.title = "Renamed"
    await session.flush()

    rows = (
        (await session.execute(select(FilmFieldChange).where(FilmFieldChange.film_id == film.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].field == "title"
    assert rows[0].old_value == "Original"
    assert rows[0].new_value == "Renamed"


async def test_denylisted_column_update_logs_nothing(session):
    film = Film(tmdb_id=990011, title="X", popularity=1.0)
    session.add(film)
    await session.flush()

    film.popularity = 99.0
    await session.flush()

    rows = (
        (await session.execute(select(FilmFieldChange).where(FilmFieldChange.film_id == film.id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_field_changed_at_reflects_a_real_update(session):
    film = Film(tmdb_id=990012, title="A")
    session.add(film)
    await session.flush()
    assert await field_changed_at(session, film.id, "title") is None

    film.title = "B"
    await session.flush()
    assert await field_changed_at(session, film.id, "title") is not None


async def test_search_fold_columns_log_nothing(session):
    """The trigger fires BEFORE UPDATE, when Postgres has not yet computed the stored fold
    columns (NEU-1469): NEW reads NULL for them, so without their denylist entry every update
    to a titled film — a popularity refresh included — would log a spurious fold change."""
    film = Film(tmdb_id=990013, title="Shōgun", original_title="將軍", popularity=1.0)
    session.add(film)
    await session.flush()

    film.popularity = 2.0
    await session.flush()
    film.title = "Shogun"
    await session.flush()

    fields = (
        (
            await session.execute(
                select(FilmFieldChange.field).where(FilmFieldChange.film_id == film.id)
            )
        )
        .scalars()
        .all()
    )
    assert fields == ["title"]
