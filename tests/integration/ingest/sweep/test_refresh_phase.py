"""The sweep's refresh phase end to end against a mocked TMDB: which films it selects,
and what it does with them.

The selection rules carry the weight here. Refresh is scoped by **reachability** — every
in-play film discover did not touch — not by datedness (§4.5), and the regression that
matters most is the *promotion gap*: a film that got a release date but stayed under the
discover popularity floor must still be refreshed, because nothing else will ever read it
again.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from tests.fixtures.tmdb import make_details
from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.sweep import run_sweep_refresh

BASE_URL = "https://api.themoviedb.org/3"
EXCLUDED = frozenset({"Released", "Canceled"})
TODAY = date(2026, 8, 10)
DORMANCY_DAYS = 365
DORMANT_REFRESH_DAYS = 30

# The last `tmdb` run began at 03:00 today, so a film discover touched carries a later
# `updated_at` and one it never reached carries an earlier one.
LAST_TMDB_RUN = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)
STALE = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
FRESH = datetime(2026, 8, 10, 4, 0, tzinfo=UTC)

DATED = TODAY + timedelta(days=90)
# Recent enough that an undated film created then is not dormant; long enough ago that one
# created at LONG_AGO is.
RECENTLY_ADMITTED = datetime(2026, 8, 1, tzinfo=UTC)
LONG_AGO = datetime(2025, 1, 1, tzinfo=UTC)
# The reduced cadence bites at TODAY - 30 days = 2026-07-11.
BEFORE_THE_CADENCE = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
async def last_tmdb_run(session):
    session.add(
        IngestRun(
            kind="tmdb",
            status="succeeded",
            started_at=LAST_TMDB_RUN,
            finished_at=LAST_TMDB_RUN + timedelta(minutes=20),
        )
    )
    await session.commit()


async def _add_undated(session, tmdb_id: int, *, updated_at: datetime, **overrides):
    return await add_film(
        session,
        tmdb_id,
        release_date=None,
        created_at=overrides.pop("created_at", RECENTLY_ADMITTED),
        updated_at=updated_at,
        **overrides,
    )


def _mock_details(tmdb_id: int, **overrides):
    return respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(
        return_value=httpx.Response(
            200,
            json=make_details(tmdb_id, **{"release_date": "", "status": "Planned", **overrides}),
        )
    )


async def _run(session_factory, tmdb_client, run_id, **overrides):
    return await run_sweep_refresh(
        session_factory=session_factory,
        client=tmdb_client,
        run_id=run_id,
        today=TODAY,
        excluded_statuses=EXCLUDED,
        dormancy_days=DORMANCY_DAYS,
        dormant_refresh_days=DORMANT_REFRESH_DAYS,
        **overrides,
    )


async def _run_row(session, run_id) -> IngestRun:
    return await session.get(IngestRun, run_id, populate_existing=True)


@respx.mock
async def test_refreshes_only_the_films_discover_did_not_touch(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    await _add_undated(session, 100, updated_at=STALE)
    await _add_undated(session, 101, updated_at=FRESH)
    await session.commit()
    _mock_details(100)
    untouched = respx.get(f"{BASE_URL}/movie/101")

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.refreshed) == (1, 1)
    assert not untouched.called, "a film discover refreshed this morning costs no request"


@respx.mock
async def test_a_dated_film_below_the_discover_floor_is_still_refreshed(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """The promotion gap (§4.5). A film that finally gets a release date is no longer
    undated, but it is still below the popularity floor where `_discover_candidate_ids`
    stops paging — so discover never reaches it either. Scoping the refresh on datedness
    would freeze its metadata permanently."""
    await add_film(
        session,
        200,
        release_date=DATED,
        popularity=0.3,
        created_at=LONG_AGO,
        updated_at=STALE,
    )
    await session.commit()
    respx.get(f"{BASE_URL}/movie/200").mock(
        return_value=httpx.Response(
            200, json=make_details(200, status="Planned", release_date=DATED.isoformat())
        )
    )

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.refreshed) == (1, 1)


@respx.mock
async def test_films_that_left_the_active_set_are_not_refreshed(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """A released or canceled film is out of play — the refresh set is scoped by
    reachability, but only over films still worth reaching."""
    await add_film(session, 300, release_date=TODAY - timedelta(days=1), updated_at=STALE)
    await _add_undated(session, 301, status="Canceled", updated_at=STALE)
    await _add_undated(session, 302, status="Released", updated_at=STALE)
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.refreshed) == (0, 0)


@respx.mock
async def test_a_dormant_film_refreshes_on_the_reduced_cadence(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """Dormancy is not an exemption. Detecting the change that revives a dormant film
    requires re-fetching it, so exempting them would make dormancy a one-way door — but
    they run on `SWEEP_DORMANT_REFRESH_DAYS`, not on every pass."""
    await _add_undated(session, 400, created_at=LONG_AGO, updated_at=BEFORE_THE_CADENCE)
    await _add_undated(session, 401, created_at=LONG_AGO, updated_at=STALE)
    await session.commit()
    _mock_details(400)
    not_yet_due = respx.get(f"{BASE_URL}/movie/401")

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.dormant_selected, result.refreshed) == (1, 1, 1)
    assert not not_yet_due.called, "a dormant film discover missed today is not due yet"


@respx.mock
async def test_the_refresh_writes_what_tmdb_now_reports(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """The whole point of the phase: no refetch means no upsert, no upsert means no
    `film_field_change` row, and no catalog-sourced event ever fires (§6.2)."""
    await _add_undated(session, 500, updated_at=STALE)
    await session.commit()
    _mock_details(500, title="Now Titled", status="In Production")

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.refreshed == 1
    row = (
        await session.execute(
            select(Film).where(Film.tmdb_id == 500).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert (row.title, row.status) == ("Now Titled", "In Production")
    assert (await _run_row(session, run_id)).items_processed == 1


@respx.mock
async def test_one_failure_does_not_roll_back_an_earlier_refresh(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    # Stalest first, so 600 (updated a day earlier) is refreshed before 601 fails.
    await _add_undated(session, 600, updated_at=STALE - timedelta(days=1))
    await _add_undated(session, 601, updated_at=STALE)
    await session.commit()
    _mock_details(600, title="Survived")
    respx.get(f"{BASE_URL}/movie/601").mock(return_value=httpx.Response(500))

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.refreshed, result.failures) == (1, 1)
    row = (
        await session.execute(
            select(Film).where(Film.tmdb_id == 600).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.title == "Survived"
    run_row = await _run_row(session, run_id)
    assert (run_row.items_processed, run_row.items_failed) == (1, 1)


@respx.mock
async def test_aborts_after_consecutive_failures_and_leaves_the_run_open(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """Same contract as the enumerate phase: a TMDB outage stops the pass rather than
    burning a request per catalog film, and the terminal status stays the entrypoint's to
    write (NEU-1079)."""
    for i, tmdb_id in enumerate((700, 701, 702)):
        await _add_undated(session, tmdb_id, updated_at=STALE - timedelta(days=3 - i))
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(return_value=httpx.Response(500))
    await _add_undated(session, 703, updated_at=STALE)
    await session.commit()
    last = respx.get(f"{BASE_URL}/movie/703")

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=3)

    assert result.aborted is True
    assert result.abort_error is not None
    assert not last.called, "the sweep must stop at the threshold, not merely report it"
    assert (await _run_row(session, run_id)).status == "running"


@respx.mock
async def test_with_no_finished_tmdb_run_every_in_play_film_is_stale(
    session, session_factory, tmdb_client, run_id
):
    """No discover pass on record at all means nothing can be assumed refreshed."""
    session.add(IngestRun(kind="tmdb", status="running", started_at=LAST_TMDB_RUN))
    await _add_undated(session, 800, updated_at=FRESH)
    await session.commit()
    _mock_details(800)

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.refreshed) == (1, 1)


@respx.mock
async def test_a_failed_tmdb_run_still_moves_the_watermark(
    session, session_factory, tmdb_client, run_id
):
    """The refresh writes, so its own upserts lift a film past whatever watermark selected
    it. If a failed `tmdb` run left the watermark behind, one pass would carry the catalog
    over it and every later pass would select nothing — silently, for the whole outage,
    which is precisely the failure this phase exists to prevent (§6.2)."""
    session.add_all(
        [
            IngestRun(
                kind="tmdb",
                status="succeeded",
                started_at=LAST_TMDB_RUN - timedelta(days=1),
                finished_at=LAST_TMDB_RUN - timedelta(days=1) + timedelta(minutes=20),
            ),
            IngestRun(
                kind="tmdb",
                status="failed",
                started_at=LAST_TMDB_RUN,
                finished_at=LAST_TMDB_RUN + timedelta(minutes=2),
            ),
        ]
    )
    # Refreshed by yesterday's sweep, so it sits above the last *successful* run's start
    # and below the failed run's.
    await _add_undated(session, 900, updated_at=STALE)
    await session.commit()
    _mock_details(900)

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.refreshed) == (1, 1)
