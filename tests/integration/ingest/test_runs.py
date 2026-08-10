from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from upmovies.ingest import runs
from upmovies.ingest.models import IngestRun


async def test_create_run_starts_running(session):
    run_id = await runs.create_run(session, kind="tmdb")
    row = (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()
    assert row.kind == "tmdb"
    assert row.status == "running"
    assert row.finished_at is None
    assert row.items_processed == 0
    assert row.items_failed == 0
    assert row.started_at is not None


async def test_record_progress_accumulates(session):
    run_id = await runs.create_run(session, kind="feeds")
    await runs.record_progress(session, run_id, processed_delta=3, failed_delta=1)
    await runs.record_progress(session, run_id, processed_delta=2)
    row = (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()
    assert row.items_processed == 5
    assert row.items_failed == 1
    assert row.last_progress_at is not None


async def test_finalize_run_sets_terminal_state(session):
    run_id = await runs.create_run(session, kind="tmdb")
    await runs.finalize_run(session, run_id, status="succeeded")
    row = (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()
    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.error is None


async def test_finalize_run_records_error(session):
    run_id = await runs.create_run(session, kind="feeds")
    await runs.finalize_run(session, run_id, status="failed", error="boom")
    row = (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()
    assert row.status == "failed"
    assert row.error == "boom"


async def test_mark_stale_runs_cancelled_only_old_running_runs(session):
    stale = IngestRun(
        kind="tmdb",
        status="running",
        started_at=datetime.now(UTC) - timedelta(minutes=120),
    )
    fresh = IngestRun(kind="feeds", status="running")
    session.add_all([stale, fresh])
    await session.flush()

    cancelled = await runs.mark_stale_runs_cancelled(session, stale_after_minutes=30)

    assert cancelled == 1
    stale_row = (
        await session.execute(select(IngestRun).where(IngestRun.id == stale.id))
    ).scalar_one()
    fresh_row = (
        await session.execute(select(IngestRun).where(IngestRun.id == fresh.id))
    ).scalar_one()
    assert stale_row.status == "cancelled"
    assert stale_row.finished_at is not None
    assert stale_row.error is not None
    assert fresh_row.status == "running"


async def test_sweep_is_an_accepted_run_kind(session):
    """The sweep gets its own run row rather than hiding inside the tmdb stage's counters,
    so `ck_ingest_run_kind` has to admit it (NEU-1074)."""
    run_id = await runs.create_run(session, kind="sweep")
    row = (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()
    assert row.kind == "sweep"


async def test_unknown_run_kind_is_rejected(session):
    """The constraint is widened, not dropped — a typo'd kind must still fail."""
    session.add(IngestRun(kind="swep", status="running"))
    with pytest.raises(IntegrityError):
        await session.flush()
