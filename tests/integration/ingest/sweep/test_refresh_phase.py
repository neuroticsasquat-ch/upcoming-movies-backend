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
from sqlalchemy import select, update

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


# --- A film TMDB has deleted is terminal, not an outage (NEU-1124) ---


@respx.mock
async def test_a_404_film_is_tombstoned_and_does_not_abort_the_pass(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """The 2026-08-11 regression, in one test.

    Every film in the set is permanently gone from TMDB. Under the old rule each 404 counted
    toward the consecutive-failure guard, so a set like this aborted the phase — and because
    the set is ordered stalest-first and a 404 never bumps `updated_at`, the same dead ids
    led the queue again the next day, forever.
    """
    for tmdb_id in range(200, 215):
        await _add_undated(session, tmdb_id, updated_at=STALE)
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(return_value=httpx.Response(404))
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=10)

    assert result.selected == 15
    assert result.missing == 15, "every 404 is counted as missing"
    assert result.failures == 0, "a deletion is not a failure"
    assert not result.aborted, "15 dead ids must not read as a TMDB outage"


@respx.mock
async def test_a_tombstoned_film_leaves_the_refresh_set(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """The half that stops the daily cost. Not counting a 404 keeps the run alive; only the
    tombstone stops it being re-fetched at the head of the queue on every later pass."""
    await _add_undated(session, 300, updated_at=STALE)
    await session.commit()
    route = respx.get(f"{BASE_URL}/movie/300").mock(return_value=httpx.Response(404))

    first = await _run(session_factory, tmdb_client, run_id)
    assert (first.selected, first.missing) == (1, 1)

    film = await session.get(Film, (await _film_id(session, 300)), populate_existing=True)
    assert film.tmdb_missing_at is not None

    second = await _run(session_factory, tmdb_client, run_id)

    assert second.selected == 0, "the tombstoned film is no longer due on the normal cadence"
    assert route.call_count == 1, "a dead id costs one request, not one per pass"


@respx.mock
async def test_a_server_error_still_counts_toward_the_abort_guard(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """The outage abort must survive the 404 carve-out — otherwise this fix trades a
    self-inflicted wedge for burning a request per catalog film during a real TMDB outage."""
    for tmdb_id in range(400, 415):
        await _add_undated(session, tmdb_id, updated_at=STALE)
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(return_value=httpx.Response(503))
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=10)

    assert result.aborted
    assert result.failures == 10
    assert result.missing == 0


@respx.mock
async def test_a_404_does_not_reset_a_run_of_real_failures(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """A deletion is no evidence TMDB has recovered. If it reset the streak, a catalog with
    dead ids sprinkled through it would blunt the outage abort exactly when it is needed."""
    for i, tmdb_id in enumerate(range(500, 512)):
        await _add_undated(session, tmdb_id, updated_at=STALE + timedelta(seconds=i))
        # A 404 sits in the middle of an otherwise unbroken run of server errors.
        status = 404 if tmdb_id == 505 else 503
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(return_value=httpx.Response(status))
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=10)

    assert result.aborted, "the 404 must not have reset the consecutive-failure count"
    assert (result.failures, result.missing) == (10, 1)


@respx.mock
async def test_a_tombstoned_film_is_rechecked_on_the_reduced_cadence(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """The tombstone must not be a one-way door. §4.5 makes exactly this argument about
    dormancy — "detecting the change that revives a dormant film requires refreshing it" — and
    a tombstone that suppressed the only reader would have no handle on the other side."""
    await _add_undated(session, 700, updated_at=STALE)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/700").mock(return_value=httpx.Response(404))

    await _run(session_factory, tmdb_client, run_id)

    # Age the tombstone past DORMANT_REFRESH_DAYS.
    await session.execute(
        update(Film).where(Film.tmdb_id == 700).values(tmdb_missing_at=BEFORE_THE_CADENCE)
    )
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.selected == 1, "a stale tombstone is due a re-check"


@respx.mock
async def test_a_still_missing_film_restamps_and_drops_back_out(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """The re-check costs one request per cadence, not one per pass — which only holds because
    confirming a film is still gone moves its tombstone forward."""
    await _add_undated(session, 710, updated_at=STALE)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/710").mock(return_value=httpx.Response(404))

    await _run(session_factory, tmdb_client, run_id)
    await session.execute(
        update(Film).where(Film.tmdb_id == 710).values(tmdb_missing_at=BEFORE_THE_CADENCE)
    )
    await session.commit()

    rechecked = await _run(session_factory, tmdb_client, run_id)
    assert (rechecked.selected, rechecked.missing) == (1, 1)

    again = await _run(session_factory, tmdb_client, run_id)
    assert again.selected == 0, "the re-check re-stamped the tombstone"


@respx.mock
async def test_a_restored_film_rejoins_the_refresh_set(
    session, session_factory, tmdb_client, run_id, last_tmdb_run
):
    """TMDB restores and re-merges deleted entries. The re-check is what gets us back in front
    of one; a successful upsert then clears the tombstone for good."""
    await _add_undated(session, 600, updated_at=STALE)
    await session.commit()
    route = respx.get(f"{BASE_URL}/movie/600").mock(return_value=httpx.Response(404))

    await _run(session_factory, tmdb_client, run_id)

    route.mock(
        return_value=httpx.Response(200, json=make_details(600, release_date="", status="Planned"))
    )
    await session.execute(
        update(Film).where(Film.tmdb_id == 600).values(tmdb_missing_at=BEFORE_THE_CADENCE)
    )
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.refreshed) == (1, 1)
    film = await session.get(Film, (await _film_id(session, 600)), populate_existing=True)
    assert film.tmdb_missing_at is None, "a successful upsert clears the tombstone"


async def _film_id(session, tmdb_id: int):
    return (await session.execute(select(Film.id).where(Film.tmdb_id == tmdb_id))).scalar_one()
