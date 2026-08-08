"""Reads that assemble candidate-retrieval health for the admin surface (NEU-997).

The soft tier of the two-tier guard (ADR-0010): drift that does not warrant failing a run
still needs somewhere to be visible. The hard tier is `link/retrieval/health.py`'s
`hard_breach_error`, which fails the run outright — and is deliberately set to catch a
collapse rather than drift, which is what leaves this surface a job to do.

**Two tables, one figure.** `RunRetrievalHealth` carries the per-run rates, counted over
every story retrieval ran over. Recall cannot come from there — it is measured against the
roster's picks, which live one row per story in `LinkRetrievalProbe`. So the recall half is
a grouped aggregate, joined back in here, rather than a column.

**The health row is what the surface keys off.** It is written even for a run with nothing
pending, so a run with no row almost always did not run retrieval at all — and gets `None`,
not a row of zeroes that would read as a run whose retrieval found nothing. The exception is
that `ShadowObserver.record_health` is best-effort by contract: a run whose health write
failed leaves probe rows behind an absent row, and reads here as a run shadow never touched.
Accepted rather than papered over — the writer's degradation is deliberate, and the same
outage would have taken the probe writes with it.

Counting the probes in SQL rather than loading them: a page of 200 runs at tens of probe
rows each is thousands of rows fetched to be immediately reduced to two integers per run.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.dto import RetrievalHealthOut, RetrievalHealthPointOut
from upmovies.ingest.models import IngestRun, LinkRetrievalProbe, RunRetrievalHealth


async def _probe_totals(db: AsyncSession, run_ids: Sequence[UUID]) -> dict[UUID, tuple[int, int]]:
    """`(roster picks, of those retrieved)` per run, for runs that have probe rows.

    Runs absent from the result linked nothing the roster could be measured against, which
    is a legitimate outcome rather than missing data — the caller reads it as `(0, 0)`."""
    if not run_ids:
        return {}
    rows = await db.execute(
        select(
            LinkRetrievalProbe.run_id,
            func.count().label("picks"),
            func.count().filter(LinkRetrievalProbe.retrieved).label("retrieved"),
        )
        .where(LinkRetrievalProbe.run_id.in_(run_ids))
        .group_by(LinkRetrievalProbe.run_id)
    )
    return {run_id: (picks, retrieved) for run_id, picks, retrieved in rows}


def _fields(health: RunRetrievalHealth, totals: tuple[int, int]) -> dict:
    picks, retrieved = totals
    return {
        "stories_retrieved": health.stories_retrieved,
        "zero_candidate_stories": health.zero_candidate_stories,
        "saturated_stories": health.saturated_stories,
        "mean_candidates": health.mean_candidates,
        "roster_picks": picks,
        "roster_picks_retrieved": retrieved,
    }


async def load_retrieval_health(
    db: AsyncSession, run_ids: Sequence[UUID]
) -> dict[UUID, RetrievalHealthOut]:
    """Retrieval health for each of `run_ids` that has any — keyed by run id.

    Takes the ids rather than the loaded runs so the caller's rows need no `retrieval_health`
    relationship loaded, and so one page of runs costs two queries whatever its size."""
    if not run_ids:
        return {}
    health_rows = (
        (await db.execute(select(RunRetrievalHealth).where(RunRetrievalHealth.run_id.in_(run_ids))))
        .scalars()
        .all()
    )
    totals = await _probe_totals(db, [h.run_id for h in health_rows])
    return {
        h.run_id: RetrievalHealthOut(**_fields(h, totals.get(h.run_id, (0, 0))))
        for h in health_rows
    }


async def retrieval_health_series(
    db: AsyncSession, *, limit: int, since: datetime | None = None
) -> list[RetrievalHealthPointOut]:
    """The most recent runs that ran retrieval, newest first — `since` onwards, `limit` at most.

    Ordered by the run's start rather than the health row's `created_at`, so the series reads
    against the ingest chain's daily cadence — the health row is written at the end of the
    stage, which reorders runs that overlap. Run id breaks ties: two runs starting in the same
    instant would otherwise order arbitrarily, and `limit` would drop a different one each
    call, which is unreadable in a series meant to be compared against itself.

    `since` is what makes a *fortnight* expressible. `limit` alone only approximates one, and
    the approximation is wrong on exactly the days that matter — a backfill or a retry adds
    runs and silently shortens the window being looked at."""
    query = (
        select(RunRetrievalHealth, IngestRun)
        .join(IngestRun, IngestRun.id == RunRetrievalHealth.run_id)
        .order_by(IngestRun.started_at.desc(), IngestRun.id.desc())
        .limit(limit)
    )
    if since is not None:
        query = query.where(IngestRun.started_at >= since)
    rows = (await db.execute(query)).all()
    totals = await _probe_totals(db, [health.run_id for health, _ in rows])
    return [
        RetrievalHealthPointOut(
            run_id=run.id,
            run_status=run.status,
            started_at=run.started_at,
            **_fields(health, totals.get(health.run_id, (0, 0))),
        )
        for health, run in rows
    ]
