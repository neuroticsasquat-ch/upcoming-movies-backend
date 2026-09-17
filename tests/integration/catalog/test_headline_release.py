"""`catalog.headline_release` (NEU-1397): which single date a one-date row leads with.

The table below is the spec's scenario table. Each case fixes a film's release rows relative to
`TODAY` and pins the one date the module must choose, so the three rules — earliest upcoming,
else most recent past, else the primary — and the same-day tie-break are all held in place by
name rather than by a reader's reconstruction of the ORDER BY.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fixtures.catalog import add_film
from upmovies.catalog.headline_release import HeadlineRelease, headline_releases
from upmovies.catalog.models import FilmReleaseDate

TODAY = date(2026, 9, 17)


def _at(days: int) -> datetime:
    """The timestamptz TMDB would store for a release `days` from `TODAY`."""
    return datetime.combine(TODAY + timedelta(days=days), datetime.min.time(), tzinfo=UTC)


async def _film(
    session,
    *,
    tmdb_id: int = 1,
    origin: list[str] | None,
    rows: list[tuple[str, int, int]],
    primary: int | None = None,
):
    """A film with `rows` of `(country, release_type, days from TODAY)` and an optional primary
    date, also given as days from `TODAY`."""
    film = await add_film(
        session,
        tmdb_id=tmdb_id,
        origin_country=origin,
        release_date=None if primary is None else TODAY + timedelta(days=primary),
    )
    for iso_3166_1, release_type, offset in rows:
        session.add(
            FilmReleaseDate(
                film_id=film.id,
                iso_3166_1=iso_3166_1,
                release_type=release_type,
                release_date=_at(offset),
            )
        )
    await session.flush()
    return film


CASES = [
    pytest.param(
        ["FR"],
        [("US", 2, 10), ("US", 3, 24), ("FR", 2, 17)],
        None,
        (10, "upcoming", "US", "limited"),
        id="earliest-upcoming-wins-even-when-it-is-a-limited-opening",
    ),
    pytest.param(
        ["DE"],
        [("US", 3, 24), ("DE", 3, 3)],
        None,
        (3, "upcoming", "DE", "wide"),
        id="an-origin-country-date-can-beat-the-us-one",
    ),
    pytest.param(
        ["US"],
        [("US", 3, 24), ("DE", 3, 3)],
        None,
        (24, "upcoming", "US", "wide"),
        id="a-non-origin-country-date-is-not-displayable",
    ),
    pytest.param(
        ["US"],
        [("US", 2, -40), ("US", 3, -26)],
        None,
        (-26, "released", "US", "wide"),
        id="all-past-falls-back-to-the-most-recent",
    ),
    pytest.param(
        ["US"],
        [("US", 2, -40), ("US", 3, 5)],
        None,
        (5, "upcoming", "US", "wide"),
        id="a-past-limited-run-does-not-hide-the-next-date",
    ),
    pytest.param(
        ["US"],
        [("US", 3, 0)],
        None,
        (0, "upcoming", "US", "wide"),
        id="today-counts-as-upcoming",
    ),
    pytest.param(
        ["US"],
        [("US", 3, 9), ("US", 3, 9), ("US", 2, 9)],
        None,
        (9, "upcoming", "US", "wide"),
        id="a-same-day-tie-breaks-wide-first-not-arbitrarily",
    ),
    pytest.param(
        ["US"],
        [("DE", 3, 200)],
        200,
        (200, "primary", None, None),
        id="nothing-displayable-falls-back-to-the-primary-date",
    ),
    pytest.param(
        ["US"],
        [("US", 1, 2)],
        2,
        (2, "primary", None, None),
        id="a-premiere-is-not-displayable-so-the-primary-answers",
    ),
    pytest.param(
        ["US"],
        [],
        None,
        None,
        id="no-rows-and-no-primary-date-has-no-headline-release",
    ),
    pytest.param(
        None,
        [("US", 3, 12)],
        None,
        (12, "upcoming", "US", "wide"),
        id="a-film-with-no-origin-country-still-gets-its-us-dates",
    ),
]


@pytest.mark.parametrize("origin,rows,primary,expected", CASES)
async def test_headline_release_scenarios(session, origin, rows, primary, expected):
    film = await _film(session, origin=origin, rows=rows, primary=primary)

    resolved = await headline_releases(session, [film.id], today=TODAY)

    if expected is None:
        assert resolved == {}
        return
    offset, kind, country, bucket = expected
    assert resolved[film.id] == HeadlineRelease(
        date=TODAY + timedelta(days=offset), kind=kind, country=country, bucket=bucket
    )


async def test_one_query_resolves_a_whole_list_and_drops_nobody(session):
    # The list endpoints' shape: a film without a headline release is simply absent from the
    # dict, so the caller can keep its row and render the absence rather than lose it.
    dated = await _film(session, tmdb_id=1, origin=["US"], rows=[("US", 3, 7)])
    undated = await _film(session, tmdb_id=2, origin=["US"], rows=[])

    resolved = await headline_releases(session, [dated.id, undated.id], today=TODAY)

    assert resolved == {
        dated.id: HeadlineRelease(
            date=TODAY + timedelta(days=7), kind="upcoming", country="US", bucket="wide"
        )
    }


async def test_no_film_ids_asks_the_database_nothing(session):
    assert await headline_releases(session, [], today=TODAY) == {}
