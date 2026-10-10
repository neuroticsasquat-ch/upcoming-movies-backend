"""The release-date poll end to end against a mocked TMDB: which films it reads, what history it
writes, and the card the sweep makes of it the next morning (D-26, NEU-1532).

The released half of the provider poll's set is the only place a US home-release date set after
a film opened can be seen at all — the refresh phase stops reading a film at release — so the
end-to-end test at the bottom is the beat this pass exists for.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from tests.fixtures.tmdb import make_release_dates
from upmovies.app.models import Follow
from upmovies.catalog.models import Film, FilmReleaseDate, FilmReleaseDateChange
from upmovies.ingest import runs
from upmovies.ingest.release_dates import run_release_date_poll
from upmovies.ingest.sweep import run_release_date_events
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.news.models import Event, EventSummary

BASE_URL = "https://api.themoviedb.org/3"
TODAY = date(2026, 9, 17)
MIN_AGE = 14
MAX_AGE = 365
EXCLUDED_STATUSES = frozenset({"Released", "Canceled"})
IN_WINDOW = TODAY - timedelta(days=60)
UPCOMING = TODAY + timedelta(days=200)
WIDE = 3
DIGITAL = 4
# Far enough out that the change row — stamped by the database's own `now()`, not by `TODAY` —
# is never observed after it: a date already past when observed cards nothing (D-1532.6).
DIGITAL_DATE = date(2036, 3, 14)
OBSERVED = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture
async def tmdb_client():
    async with TMDBClient(
        base_url=BASE_URL,
        api_key="test-key",
        rate_calls=100,
        rate_window=1,
        retry_max_attempts=2,
        retry_base_delay=0.01,
    ) as client:
        yield client


@pytest.fixture
async def run_id(session):
    run = await runs.create_run(session, kind="providers")
    await session.commit()
    return run


@pytest.fixture
async def follower(make_user):
    return await make_user(email="follower@example.com")


async def _add_released_film(session, tmdb_id: int, *, observed: bool = True, **overrides) -> Film:
    """A film that opened 60 days ago — in the poll set on rule 1 alone — holding its stored US
    wide row, and observed before it opened unless `observed=False`."""
    defaults = {
        "status": "Released",
        "release_date": IN_WINDOW,
        "release_dates_observed_at": OBSERVED if observed else None,
    }
    film = await add_film(session, tmdb_id, **{**defaults, **overrides})
    session.add(
        FilmReleaseDate(
            film_id=film.id,
            iso_3166_1="US",
            release_type=WIDE,
            release_date=datetime.combine(IN_WINDOW, datetime.min.time(), tzinfo=UTC),
        )
    )
    await session.flush()
    return film


async def _follow(session, user, film: Film) -> None:
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.flush()


def _with_digital(tmdb_id: int) -> dict:
    return make_release_dates(
        tmdb_id, ("US", WIDE, IN_WINDOW.isoformat()), ("US", DIGITAL, DIGITAL_DATE.isoformat())
    )


def _mock_release_dates(tmdb_id: int, payload: dict | None = None):
    return respx.get(f"{BASE_URL}/movie/{tmdb_id}/release_dates").mock(
        return_value=httpx.Response(200, json=payload or make_release_dates(tmdb_id))
    )


async def _run(session_factory, tmdb_client, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "client": tmdb_client,
        "run_id": run_id,
        "today": TODAY,
        "min_age_days": MIN_AGE,
        "max_age_days": MAX_AGE,
        "excluded_statuses": EXCLUDED_STATUSES,
    }
    return await run_release_date_poll(**{**kwargs, **overrides})


async def _changes(session, film: Film) -> list[FilmReleaseDateChange]:
    rows = await session.execute(
        select(FilmReleaseDateChange)
        .where(FilmReleaseDateChange.film_id == film.id)
        .order_by(FilmReleaseDateChange.id)
    )
    return list(rows.scalars())


async def _stored(session, film: Film) -> list[tuple[str, int, date]]:
    rows = await session.execute(
        select(
            FilmReleaseDate.iso_3166_1, FilmReleaseDate.release_type, FilmReleaseDate.release_date
        )
        .where(FilmReleaseDate.film_id == film.id)
        .order_by(FilmReleaseDate.release_type)
    )
    return [(iso, kind, when.date()) for iso, kind, when in rows]


# --- what it writes ------------------------------------------------------------


@respx.mock
async def test_a_us_digital_date_set_after_release_is_recorded(
    session, session_factory, tmdb_client, run_id
):
    """The beat D-26 promised and nothing could deliver: the refresh phase stopped reading the
    film the day it opened."""
    film = await _add_released_film(session, 200)
    await session.commit()
    _mock_release_dates(200, _with_digital(200))

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled, result.changes, result.baselined) == (1, 1, 1, 0)
    (change,) = await _changes(session, film)
    assert (change.change, change.iso_3166_1, change.release_type) == ("set", "US", DIGITAL)
    assert change.new_date == DIGITAL_DATE
    assert await _stored(session, film) == [
        ("US", WIDE, IN_WINDOW),
        ("US", DIGITAL, DIGITAL_DATE),
    ]


@respx.mock
async def test_a_first_observation_is_a_baseline(session, session_factory, tmdb_client, run_id):
    """ADR-0014, by the rebuild's own rule: rows written, nothing recorded."""
    film = await _add_released_film(session, 201, observed=False)
    await session.commit()
    _mock_release_dates(201, _with_digital(201))

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.polled, result.changes, result.baselined) == (1, 0, 1)
    assert await _changes(session, film) == []
    assert len(await _stored(session, film)) == 2
    await session.refresh(film)
    assert film.release_dates_observed_at is not None


@respx.mock
async def test_an_unchanged_slate_writes_no_change_on_a_second_pass(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 202)
    await session.commit()
    _mock_release_dates(202, _with_digital(202))
    await _run(session_factory, tmdb_client, run_id)

    second = await _run(session_factory, tmdb_client, run_id)

    assert (second.polled, second.changes) == (1, 0)
    assert len(await _changes(session, film)) == 1


@respx.mock
async def test_it_writes_release_dates_and_nothing_else(
    session, session_factory, tmdb_client, run_id
):
    """No full upsert (D-1532.4): a released film's status, title and dates stay as stored,
    whatever TMDB would say about them, so no other card can come of this read."""
    film = await _add_released_film(session, 203, status="Post Production", title="Stored")
    await session.commit()
    _mock_release_dates(203, _with_digital(203))

    await _run(session_factory, tmdb_client, run_id)

    await session.refresh(film)
    assert (film.status, film.title, film.release_date) == ("Post Production", "Stored", IN_WINDOW)


# --- which films it reads ------------------------------------------------------


@respx.mock
async def test_a_followed_unreleased_film_is_not_read(
    session, session_factory, tmdb_client, run_id, follower
):
    """That film is the video poll's; the refresh phase still reads its release dates."""
    film = await add_film(session, 210, status="Post Production", release_date=UPCOMING)
    await _follow(session, follower, film)
    await session.commit()
    route = _mock_release_dates(210)

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.selected == 0
    assert route.call_count == 0


@respx.mock
async def test_a_followed_released_film_with_no_theatrical_row_is_read(
    session, session_factory, tmdb_client, run_id, follower
):
    """Rule 2: straight to streaming, so no theatrical date puts it in the window — the follow
    does, and the film has opened, so it is this half's."""
    film = await add_film(session, 211, status="Released", release_date=TODAY - timedelta(days=3))
    await _follow(session, follower, film)
    await session.commit()
    route = _mock_release_dates(211)

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)
    assert route.call_count == 1


@respx.mock
async def test_a_tombstoned_film_is_not_read(session, session_factory, tmdb_client, run_id):
    film = await _add_released_film(session, 212)
    film.tmdb_missing_at = datetime.now(UTC)
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.selected == 0


# --- failure handling ----------------------------------------------------------


@respx.mock
async def test_a_404_tombstones_the_film_rather_than_counting_a_failure(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 220)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/220/release_dates").mock(return_value=httpx.Response(404))

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.missing, result.failures, result.polled) == (1, 0, 0)
    await session.refresh(film)
    assert film.tmdb_missing_at is not None


@respx.mock
async def test_a_sustained_outage_aborts_the_pass(session, session_factory, tmdb_client, run_id):
    for tmdb_id in (221, 222, 223):
        await _add_released_film(session, tmdb_id)
    await session.commit()
    for tmdb_id in (221, 222, 223):
        respx.get(f"{BASE_URL}/movie/{tmdb_id}/release_dates").mock(
            return_value=httpx.Response(500)
        )

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=2)

    assert result.aborted is True
    assert result.failures == 2
    assert "2 consecutive failures" in (result.abort_error or "")


# --- end to end: the sweep cards what this pass recorded -----------------------


@respx.mock
async def test_the_next_sweep_cards_the_home_release_date(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 230)
    await session.commit()
    _mock_release_dates(230, _with_digital(230))
    await _run(session_factory, tmdb_client, run_id)
    (change,) = await _changes(session, film)

    carded = await run_release_date_events(
        session_factory=session_factory,
        run_id=run_id,
        now=change.changed_at + timedelta(days=1),
        lookback_days=7,
        corroboration_window_days=14,
    )

    assert carded.events_created == 1
    (event,) = (
        (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    )
    assert event.event_type == "release_date"
    assert event.subject_key == ["US:digital"]
    assert event.occurred_at == change.changed_at
    summary = await session.get(EventSummary, event.id)
    assert summary is not None
    assert summary.summary == "US digital release date set to 14 March 2036."
