"""What the sweep's phases share: a session per item, the consecutive-failure abort, and the
liveness heartbeat.

None is specific to enumerating, refreshing, or carding — they are the pipeline contract the
sweep inherits from `run_tmdb_ingest` (commit per item so one failure never rolls back the
others; stop on an outage rather than burning a request per catalog film) plus the one thing
the sweep needs and the shorter pipelines do not: a way to say "still alive" during the long
stretches where it legitimately produces nothing. Keeping one copy of each is what stops them
drifting into three different definitions of "gave up" and "still going".
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.runs import record_progress, touch_run
from upmovies.ingest.sweep.seeds import SessionFactory

# Long enough that 7,519 seed people cost ~30 writes rather than 7,519, short enough that
# `INGEST_STALE_RUN_MINUTES` has orders of magnitude of headroom above it.
_HEARTBEAT_SECONDS = 60.0


@asynccontextmanager
async def owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """One short-lived session, so a per-item commit cannot take another item with it."""
    async with session_factory() as s:
        yield s


@dataclass
class AbortGuard:
    """Consecutive-failure abort, shared by a phase's loops so an outage that starts in one
    is still counted in the other. Records each failure against the run as it goes."""

    session_factory: SessionFactory
    run_id: UUID
    threshold: int
    consecutive: int = 0

    async def failed(self) -> bool:
        """Record one failure; True when the run has hit the consecutive threshold."""
        self.consecutive += 1
        async with owned_session(self.session_factory) as s:
            await record_progress(s, self.run_id, failed_delta=1)
            await s.commit()
        return self.consecutive >= self.threshold

    def succeeded(self) -> None:
        self.consecutive = 0


@dataclass
class Heartbeat:
    """Says *this run is still alive* on a throttle, for loops that can legitimately run for
    an hour without producing anything.

    `mark_stale_runs_cancelled` expires a run on `last_progress_at`, and enumerate's person
    loop issues one credits request per seed person while admitting nothing — so the column
    that decides whether the run is an orphan stays NULL through exactly its longest stretch.
    That is the 2026-08-11 incident (NEU-1117). Every per-item loop in the sweep ticks this
    regardless of the item's outcome, which is what makes one tight staleness window safe for
    a six-hour run.

    Throttled in **time**, not in items: the window it feeds is a time window, so the two are
    reasoned about together. A per-N-items throttle would make the guarantee depend on how
    fast a particular loop happens to be — 250 seed people is about a minute, 250 event rows
    is a blink — and one slow item would break it outright.

    The first tick always writes. A run that dies in its first minute would otherwise be
    indistinguishable from one that never started, and NULL is the case the incident was made
    of.
    """

    session_factory: SessionFactory
    run_id: UUID
    interval_seconds: float = _HEARTBEAT_SECONDS
    _last_tick: float | None = field(default=None, init=False, repr=False)

    async def tick(self) -> None:
        now = time.monotonic()
        if self._last_tick is not None and now - self._last_tick < self.interval_seconds:
            return
        self._last_tick = now
        async with owned_session(self.session_factory) as s:
            await touch_run(s, self.run_id)
            await s.commit()
