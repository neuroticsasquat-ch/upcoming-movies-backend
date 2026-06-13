from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from upmovies.ingest.models import IngestRun
from upmovies.main import run_startup_cleanup


async def test_startup_cleanup_cancels_stale_running_runs(session):
    stale = IngestRun(
        kind="tmdb",
        status="running",
        started_at=datetime.now(UTC) - timedelta(minutes=60),
    )
    fresh = IngestRun(kind="feeds", status="running", started_at=datetime.now(UTC))
    session.add_all([stale, fresh])
    await session.commit()

    cancelled = await run_startup_cleanup(session, stale_after_minutes=15)
    await session.commit()

    assert cancelled == 1
    rows = {
        r.kind: r.status
        for r in (
            await session.execute(select(IngestRun), execution_options={"populate_existing": True})
        ).scalars()
    }
    assert rows["tmdb"] == "cancelled"
    assert rows["feeds"] == "running"
