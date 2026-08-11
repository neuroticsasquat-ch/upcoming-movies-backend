"""What the sweep's phases share: a session per item, and the consecutive-failure abort.

Neither is specific to enumerating, refreshing, or carding — they are the pipeline contract the
sweep inherits from `run_tmdb_ingest` (commit per item so one failure never rolls back the
others; stop on an outage rather than burning a request per catalog film). Keeping one copy
is what stops them drifting into three different definitions of "gave up".
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.seeds import SessionFactory


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
