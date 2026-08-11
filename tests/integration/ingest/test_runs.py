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


async def test_touch_run_advances_last_progress_at_without_touching_counters(session):
    """The heartbeat says "alive", not "produced something" — so it must not move the
    counters `/admin/runs` reads as work done (NEU-1117)."""
    run_id = await runs.create_run(session, kind="sweep")
    await runs.touch_run(session, run_id)
    row = (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()
    assert row.last_progress_at is not None
    assert (row.items_processed, row.items_failed) == (0, 0)


async def test_a_heartbeating_run_survives_an_old_started_at(session):
    """The whole point: a sweep that has been running for hours but is demonstrably alive
    is not an orphan. Under the old `started_at` rule this row was cancelled."""
    alive = IngestRun(
        kind="sweep",
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=6),
        last_progress_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    session.add(alive)
    await session.flush()

    assert await runs.mark_stale_runs_cancelled(session, stale_after_minutes=30) == 0

    row = (
        await session.execute(
            select(IngestRun)
            .where(IngestRun.id == alive.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "running"


async def test_a_run_whose_heartbeat_went_quiet_is_cancelled(session):
    quiet = IngestRun(
        kind="sweep",
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=6),
        last_progress_at=datetime.now(UTC) - timedelta(hours=2),
    )
    session.add(quiet)
    await session.flush()

    assert await runs.mark_stale_runs_cancelled(session, stale_after_minutes=30) == 1

    row = (
        await session.execute(
            select(IngestRun)
            .where(IngestRun.id == quiet.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "cancelled"
    assert row.finished_at is not None


async def test_a_null_last_progress_at_falls_back_to_started_at(session):
    """The incident case (2026-08-11): a sweep killed mid-enumerate by the scheduled-task
    timeout never reached its first admission, so `last_progress_at` was NULL. It must
    still expire on `started_at` rather than linger forever."""
    orphan = IngestRun(
        kind="sweep",
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=6),
    )
    fresh = IngestRun(kind="sweep", status="running")
    session.add_all([orphan, fresh])
    await session.flush()
    assert orphan.last_progress_at is None

    assert await runs.mark_stale_runs_cancelled(session, stale_after_minutes=30) == 1

    orphan_row = (
        await session.execute(select(IngestRun).where(IngestRun.id == orphan.id))
    ).scalar_one()
    fresh_row = (
        await session.execute(select(IngestRun).where(IngestRun.id == fresh.id))
    ).scalar_one()
    assert orphan_row.status == "cancelled"
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


async def test_last_finished_run_started_at_ignores_other_kinds_and_running_runs(session):
    """The sweep's refresh watermark. A run still `running` started moments ago and would
    mark the whole catalog stale, so it does not count; a run of another kind says nothing
    about what discover reached."""
    latest_finished = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)
    session.add_all(
        [
            IngestRun(kind="tmdb", status="succeeded", started_at=datetime(2026, 8, 1, tzinfo=UTC)),
            IngestRun(kind="tmdb", status="succeeded", started_at=latest_finished),
            IngestRun(kind="tmdb", status="running", started_at=datetime(2026, 8, 11, tzinfo=UTC)),
            IngestRun(
                kind="feeds", status="succeeded", started_at=datetime(2026, 8, 11, tzinfo=UTC)
            ),
        ]
    )
    await session.flush()

    assert await runs.last_finished_run_started_at(session, "tmdb") == latest_finished


async def test_last_finished_run_started_at_counts_a_run_that_failed(session):
    """Failed counts, and the refresh phase depends on it. The refresh writes, so its own
    upserts lift a film past whatever watermark selected it — pin the watermark to the last
    *success* and a broken `tmdb` stage freezes it, and the refresh silently selects nothing
    for as long as the outage lasts."""
    failed = datetime(2026, 8, 10, 3, 0, tzinfo=UTC)
    session.add_all(
        [
            IngestRun(kind="tmdb", status="succeeded", started_at=datetime(2026, 8, 9, tzinfo=UTC)),
            IngestRun(kind="tmdb", status="failed", started_at=failed),
        ]
    )
    await session.flush()

    assert await runs.last_finished_run_started_at(session, "tmdb") == failed


async def test_last_finished_run_started_at_is_none_when_none_has_finished(session):
    session.add(IngestRun(kind="tmdb", status="running"))
    await session.flush()

    assert await runs.last_finished_run_started_at(session, "tmdb") is None
