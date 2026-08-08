"""`build_candidate_index(as_of=...)` reconstructs the active set as it stood on a past date.

The eval harness scores a fixture whose stories are news from the weeks around its labeling
date. Films release continuously — 22% of the active catalog turns over in a month — so an
index built at wall-clock time is missing the very films the fixture's stories are about, and
those items score as recall failures of the path under test rather than as catalog drift.
Pinning the date makes the fixture a stable oracle instead of a decaying one.
"""

from datetime import date

from upmovies.catalog.models import Film
from upmovies.link.retrieval.index import build_candidate_index


async def test_as_of_keeps_a_film_that_has_since_released(session):
    """The case the harness actually hits: labeled while upcoming, released since."""
    film = Film(tmdb_id=1, title="Nagabandham", release_date=date(2026, 7, 28))
    session.add(film)
    await session.commit()

    # Wall clock: the film released, so it is out of scope and unreachable.
    today = await build_candidate_index(session, as_of=date(2026, 8, 8))
    assert today.size == 0
    assert today.films_for_token("nagabandham") == frozenset()

    # Pinned to when the story was news: in scope, exactly as it was then.
    pinned = await build_candidate_index(session, as_of=date(2026, 7, 1))
    assert pinned.size == 1
    assert pinned.films_for_token("nagabandham") == frozenset({film.id})


async def test_as_of_still_excludes_films_released_before_that_date(session):
    """Pinning rewinds the clock; it does not widen scope to released films."""
    session.add(Film(tmdb_id=2, title="Already Out", release_date=date(2026, 6, 1)))
    await session.commit()

    index = await build_candidate_index(session, as_of=date(2026, 7, 1))

    assert index.size == 0


async def test_as_of_does_not_override_excluded_status(session):
    """Status exclusion is not date-dependent — a canceled film stays out at any as_of."""
    session.add(
        Film(
            tmdb_id=3,
            title="Shelved Forever",
            release_date=date(2027, 1, 1),
            status="Canceled",
        )
    )
    await session.commit()

    index = await build_candidate_index(session, as_of=date(2026, 7, 1))

    assert index.size == 0


async def test_omitting_as_of_uses_wall_clock(session):
    """The production call site passes nothing and must keep its current behaviour."""
    session.add(
        Film(tmdb_id=4, title="Still Upcoming", release_date=date(date.today().year + 1, 7, 15))
    )
    session.add(Film(tmdb_id=5, title="Long Released", release_date=date(2000, 1, 1)))
    await session.commit()

    index = await build_candidate_index(session)

    assert index.size == 1
