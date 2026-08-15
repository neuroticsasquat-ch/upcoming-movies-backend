from datetime import UTC, date, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film, FilmFieldChange
from upmovies.catalog.queries import active_film_clause, dormant_film_clause
from upmovies.news.models import Story

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
        select(Film.title).where(
            active_film_clause(today=TODAY, excluded_statuses=EXCLUDED, dormancy_days=365)
        )
    )

    assert set(rows.scalars().all()) == {"Future Normal", "Undated Unknown"}


DORMANCY_DAYS = 90
# TODAY - 90 days is 2026-04-03; anything quiet since before that is dormant.
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)
RECENT = datetime(2026, 6, 1, tzinfo=UTC)


def _dormancy_clause():
    return active_film_clause(today=TODAY, excluded_statuses=EXCLUDED, dormancy_days=DORMANCY_DAYS)


async def _active_titles(session) -> set[str]:
    rows = await session.execute(select(Film.title).where(_dormancy_clause()))
    return set(rows.scalars().all())


async def test_quiescent_undated_films_go_dormant(session):
    session.add_all(
        [
            # dormant: undated, admitted long ago, no change and no linked story since
            Film(tmdb_id=10, title="Quiet", release_date=None, created_at=LONG_AGO),
            # active: undated but only just admitted — quiescence is measured from
            # admission, so a new film is not born dormant
            Film(
                tmdb_id=11,
                title="Just Admitted",
                release_date=None,
                created_at=datetime(2026, 6, 20, tzinfo=UTC),
            ),
            # active: dated and in the future — dated films age out by release date,
            # dormancy never applies to them however quiet they are
            Film(
                tmdb_id=12,
                title="Dated And Quiet",
                release_date=date(2026, 12, 1),
                created_at=LONG_AGO,
            ),
        ]
    )
    await session.commit()

    assert await _active_titles(session) == {"Just Admitted", "Dated And Quiet"}


async def test_a_field_change_revives_a_dormant_film(session):
    film = Film(tmdb_id=20, title="Revived By Change", release_date=None, created_at=LONG_AGO)
    session.add(film)
    await session.commit()

    # It starts out dormant...
    assert await _active_titles(session) == set()

    # ...and a single change row brings it back, with no other intervention.
    session.add(
        FilmFieldChange(film_id=film.id, field="overview", new_value="x", changed_at=RECENT)
    )
    await session.commit()

    assert await _active_titles(session) == {"Revived By Change"}


async def test_a_linked_story_revives_a_dormant_film(session):
    film = Film(tmdb_id=30, title="Revived By Story", release_date=None, created_at=LONG_AGO)
    session.add(film)
    await session.commit()

    assert await _active_titles(session) == set()

    session.add(
        Story(
            source="test",
            url="https://example.test/revival",
            title="A story",
            film_id=film.id,
            link_status="linked",
            linked_at=RECENT,
        )
    )
    await session.commit()

    assert await _active_titles(session) == {"Revived By Story"}


async def test_stale_signals_do_not_keep_a_film_active(session):
    """Both signals must be *recent*: a change and a story from before the window
    leave the film dormant, and each is measured on its own timestamp."""
    film = Film(tmdb_id=40, title="Stale Signals", release_date=None, created_at=LONG_AGO)
    session.add(film)
    await session.commit()
    session.add_all(
        [
            FilmFieldChange(
                film_id=film.id,
                field="overview",
                new_value="x",
                changed_at=datetime(2026, 2, 1, tzinfo=UTC),
            ),
            Story(
                source="test",
                url="https://example.test/stale",
                title="An old story",
                film_id=film.id,
                link_status="linked",
                linked_at=datetime(2026, 2, 15, tzinfo=UTC),
            ),
        ]
    )
    await session.commit()

    assert await _active_titles(session) == set()


async def test_dormancy_still_defers_to_the_terminal_status_and_date_rules(session):
    """Recent activity revives a dormant film; it does not resurrect a released or
    canceled one — the dormancy term narrows the active set, never widens it."""
    film = Film(
        tmdb_id=50,
        title="Busy But Canceled",
        release_date=None,
        status="Canceled",
        created_at=LONG_AGO,
    )
    session.add(film)
    await session.commit()
    session.add(
        FilmFieldChange(film_id=film.id, field="status", new_value="Canceled", changed_at=RECENT)
    )
    await session.commit()

    assert await _active_titles(session) == set()


async def test_dormant_film_clause_never_catches_a_dated_film(session):
    """The half of `active_film_clause` the sweep's refresh phase reads on its own. Dated
    films age out by release date, so quiescence says nothing about them however quiet they
    are — and the refresh cadence a film lands on depends on getting this right."""
    session.add_all(
        [
            Film(tmdb_id=60, title="Quiet Undated", release_date=None, created_at=LONG_AGO),
            Film(
                tmdb_id=61,
                title="Quiet But Dated",
                release_date=date(2026, 12, 1),
                created_at=LONG_AGO,
            ),
            Film(tmdb_id=62, title="Busy Undated", release_date=None, created_at=RECENT),
        ]
    )
    await session.commit()

    rows = await session.execute(
        select(Film.title).where(dormant_film_clause(today=TODAY, dormancy_days=DORMANCY_DAYS))
    )

    assert set(rows.scalars().all()) == {"Quiet Undated"}
