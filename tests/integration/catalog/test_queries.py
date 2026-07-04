from datetime import date

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.catalog.queries import active_film_clause

TODAY = date(2026, 7, 2)
EXCLUDED = frozenset({"Released", "Canceled"})


async def test_active_film_clause_keeps_only_in_play_films(session):
    session.add_all(
        [
            # active: future date, non-terminal status
            Film(
                tmdb_id=1,
                title="Future Normal",
                release_date=date(2026, 12, 1),
                status="Post Production",
            ),
            # inactive: release_date < today (status frozen pre-"Released")
            Film(
                tmdb_id=2,
                title="Past Dated",
                release_date=date(2026, 1, 1),
                status="Post Production",
            ),
            # inactive: undated but terminal status
            Film(tmdb_id=3, title="Undated Released", release_date=None, status="Released"),
            # active: undated, unknown status
            Film(tmdb_id=4, title="Undated Unknown", release_date=None, status=None),
            # inactive: future date but canceled
            Film(
                tmdb_id=5, title="Canceled Future", release_date=date(2027, 1, 1), status="Canceled"
            ),
        ]
    )
    await session.commit()

    rows = await session.execute(
        select(Film.title).where(active_film_clause(today=TODAY, excluded_statuses=EXCLUDED))
    )

    assert set(rows.scalars().all()) == {"Future Normal", "Undated Unknown"}
