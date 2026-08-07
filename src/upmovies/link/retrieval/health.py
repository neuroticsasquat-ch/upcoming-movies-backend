"""Per-run retrieval health: the tally, and the one row it becomes.

Counted over **every** story retrieval ran over, not just the ones that produced a link.
That denominator is the whole point — the zero-candidate majority (ADR-0009) is invisible
to `ingest.link_retrieval_probe` by design, and it is precisely the population a retrieval
bug shows up in.

**Both retrieval paths write this row.** In `shadow` the roster still decides and the tally
measures what retrieval *would* have offered; under `on` the same numbers describe what it
actually offered. Keeping them one shape is what lets a shadow period and the live stage be
read against each other — and is why this lives here rather than in `link/shadow.py`, which
M4 deletes along with the roster path (NEU-1004) while the health row must survive to feed
the hard-breach guard (NEU-1002).

**The write never raises.** A telemetry row is not worth failing a run whose stories are
already decided, and under `on` those decisions are final — `link` is lossy, so a run failed
after the fact would re-run over an empty backlog and change nothing. Stated once here so
neither caller has to restate it.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.models import RunRetrievalHealth
from upmovies.link.retrieval.select import CandidateSet

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@dataclass
class RetrievalTally:
    """The per-run aggregates, accumulated over every story retrieval ran over.

    Kept as counters rather than derived from the probe rows because those hold only the
    roster-linked minority — the zero-candidate stories that dominate the rate are absent
    from that table by design."""

    stories_retrieved: int = 0
    zero_candidate_stories: int = 0
    saturated_stories: int = 0
    candidates_offered: int = 0

    def add(self, candidates: CandidateSet) -> None:
        """Fold one story's retrieval result into the run's totals."""
        self.stories_retrieved += 1
        self.candidates_offered += len(candidates.candidates)
        if candidates.is_empty:
            self.zero_candidate_stories += 1
        if candidates.saturated:
            self.saturated_stories += 1

    @property
    def mean_candidates(self) -> float | None:
        """Mean offered-set size per story, or None when there were no stories.

        None rather than 0.0: with no denominator, a zero would read as "every story got
        zero candidates", which is the alarm this number exists to raise."""
        if not self.stories_retrieved:
            return None
        return self.candidates_offered / self.stories_retrieved


async def record_retrieval_health(
    session_factory: SessionFactory, *, run_id: UUID, tally: RetrievalTally
) -> None:
    """Write the run's aggregate row. Logs and returns on failure; never raises.

    Written once at the end of the link stage rather than incremented per batch: the row is
    a whole-run rate, and a partial one would understate the denominator it is read against.
    Written even when the run had nothing pending, so that a *missing* row keeps its own
    meaning — retrieval did not run at all."""
    try:
        async with _owned_session(session_factory) as s:
            s.add(
                RunRetrievalHealth(
                    run_id=run_id,
                    stories_retrieved=tally.stories_retrieved,
                    zero_candidate_stories=tally.zero_candidate_stories,
                    saturated_stories=tally.saturated_stories,
                    mean_candidates=tally.mean_candidates,
                )
            )
            await s.commit()
        log.info(
            "retrieval health: stories=%d zero_candidate=%d saturated=%d mean_candidates=%s",
            tally.stories_retrieved,
            tally.zero_candidate_stories,
            tally.saturated_stories,
            tally.mean_candidates,
        )
    except Exception:
        log.exception("recording retrieval health failed")
