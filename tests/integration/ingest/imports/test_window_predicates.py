"""The alert window's two spellings agree (NEU-1505, D-1505.6).

`ingest.imports.apply.release_date_in_window` is `alert_window_clause`'s date half in Python, so
an import can decline an old film from a list's date before it fetches it. A second spelling is
how a window drifts from the provider poll it is supposed to agree with, so this holds the two
to one table of dates, row for row — the drift guard `in_alert_window`'s docstring asks for.

Integration rather than unit because one side is SQL, and the only honest way to evaluate it is
to let Postgres do so."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import Film
from upmovies.catalog.queries import alert_window_clause
from upmovies.ingest.imports.apply import release_date_in_window

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


@pytest.mark.parametrize("status", ["Released", "Post Production"])
async def test_the_python_predicate_agrees_with_alert_window_clause(session, status):
    # Any status but `Canceled`, so the clause's status term is out of the way and only the
    # date half — the half the Python predicate spells — decides.
    films = {
        await add_film(session, tmdb_id=9500 + i, release_date=d, status=status): d
        for i, d in enumerate(DATES)
    }
    await session.commit()

    in_sql = set(
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

    for film, release_date in films.items():
        in_python = release_date_in_window(release_date, today=TODAY, max_age_days=MAX_AGE_DAYS)
        assert in_python is (film.id in in_sql), release_date
