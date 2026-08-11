"""The sweep's liveness heartbeat (NEU-1117).

`last_progress_at` is what `mark_stale_runs_cancelled` expires a run on, and the sweep's
enumerate phase can spend 30–70 minutes issuing one credits request per seed person without
admitting anything. These tests pin the two properties that make one tight staleness window
safe for a six-hour run: the first tick writes immediately, and later ticks are throttled so
7,500 iterations do not become 7,500 sessions.
"""

from datetime import datetime

from sqlalchemy import select

from upmovies.ingest.models import IngestRun
from upmovies.ingest.sweep.phase import Heartbeat


async def _last_progress(session, run_id) -> datetime | None:
    stmt = select(IngestRun).where(IngestRun.id == run_id).execution_options(populate_existing=True)
    row = (await session.execute(stmt)).scalar_one()
    return row.last_progress_at


async def test_the_first_tick_writes_immediately(session, session_factory, run_id):
    """No grace period on the first tick: a run that dies early still has to have said
    "alive" once, or its NULL is indistinguishable from the orphan case."""
    assert await _last_progress(session, run_id) is None

    await Heartbeat(session_factory, run_id).tick()

    assert await _last_progress(session, run_id) is not None


async def test_a_second_tick_inside_the_interval_is_suppressed(session, session_factory, run_id):
    heartbeat = Heartbeat(session_factory, run_id, interval_seconds=3600)
    await heartbeat.tick()
    first = await _last_progress(session, run_id)

    await heartbeat.tick()

    assert await _last_progress(session, run_id) == first


async def test_a_tick_after_the_interval_writes_again(session, session_factory, run_id):
    """Driven by winding the clock back rather than sleeping, and with a real interval: a
    zero interval would pass even if the elapsed-time comparison were inverted."""
    heartbeat = Heartbeat(session_factory, run_id, interval_seconds=60)
    await heartbeat.tick()
    first = await _last_progress(session, run_id)
    assert first is not None
    heartbeat._last_tick -= 61  # pyright: ignore[reportOperatorIssue]

    await heartbeat.tick()

    second = await _last_progress(session, run_id)
    assert second is not None and second > first
