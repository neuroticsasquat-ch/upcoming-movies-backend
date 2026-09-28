"""The alert window's two spellings agree (NEU-1505, D-1505.6; NEU-1510).

`ingest.imports.apply.release_date_in_window` is `alert_window_clause`'s date half in Python, so
an import can decline an old film from a list's date before it fetches it, and `film_in_window`
is the whole clause, so it can decline a fetched film before it writes it. A second spelling is
how a window drifts from the provider poll it is supposed to agree with, so this holds each to
one table of dates (and statuses), row for row — the drift guard `in_alert_window`'s docstring
asks for.

Integration rather than unit because one side is SQL, and the only honest way to evaluate it is
to let Postgres do so."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import Film
from upmovies.catalog.queries import alert_window_clause
from upmovies.ingest.imports.apply import film_in_window, release_date_in_window

TODAY = datetime.now(UTC).date()
MAX_AGE_DAYS = 365

DATES = [
    None,
    TODAY - timedelta(days=MAX_AGE_DAYS + 1),
    TODAY - timedelta(days=MAX_AGE_DAYS),
    TODAY - timedelta(days=MAX_AGE_DAYS - 1),
    TODAY,
    TODAY + timedelta(days=1),
]

STATUSES = [None, "Released", "Post Production", "Canceled"]


async def _in_sql(session, films) -> set:
    return set(
        (
            await session.execute(
                select(Film.id).where(
                    Film.id.in_([f.id for f in films]),
                    alert_window_clause(today=TODAY, max_age_days=MAX_AGE_DAYS),
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.parametrize("status", ["Released", "Post Production"])
async def test_the_python_predicate_agrees_with_alert_window_clause(session, status):
    # Any status but `Canceled`, so the clause's status term is out of the way and only the
    # date half — the half the Python predicate spells — decides.
    films = {
        await add_film(session, tmdb_id=9500 + i, release_date=d, status=status): d
        for i, d in enumerate(DATES)
    }
    await session.commit()

    in_sql = await _in_sql(session, films)

    for film, release_date in films.items():
        in_python = release_date_in_window(release_date, today=TODAY, max_age_days=MAX_AGE_DAYS)
        assert in_python is (film.id in in_sql), release_date


async def test_the_whole_window_predicate_agrees_with_alert_window_clause(session):
    # NEU-1510: the date half and the status half together, the way a fetched film is judged
    # before it is written. A `None` status is in, as the clause's NULL guard keeps it.
    films = {
        await add_film(session, tmdb_id=9600 + i, release_date=d, status=s): (d, s)
        for i, (d, s) in enumerate((d, s) for d in DATES for s in STATUSES)
    }
    await session.commit()

    in_sql = await _in_sql(session, films)

    for film, (release_date, status) in films.items():
        in_python = film_in_window(release_date, status, today=TODAY, max_age_days=MAX_AGE_DAYS)
        assert in_python is (film.id in in_sql), (release_date, status)
