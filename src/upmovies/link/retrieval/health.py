"""Per-run retrieval health: the tally, the one row it becomes, and the hard-breach rule.

Counted over **every** story retrieval ran over, not just the ones that produced a link.
That denominator is the whole point — the zero-candidate majority (ADR-0009) is invisible
to `ingest.link_retrieval_probe` by design, and it is precisely the population a retrieval
bug shows up in.

**One shape across the rollout.** The shadow observer wrote this same row while the roster
still decided, which is what let the shadow period and the live stage be read against each
other. That is why the row lived here rather than in `link/shadow.py` — M4 deleted that
module with the rest of the roster path (NEU-1004), and the health row survived it to feed
the hard-breach guard (NEU-1002).

**The write never raises.** A telemetry row is not worth failing a run whose stories are
already decided, and those decisions are final — `link` is lossy, so a run failed after the
fact would re-run over an empty backlog and change nothing.

**`hard_breach_error` is the other half** — the hard tier of ADR-0010's two-tier guard, and
the replacement for the cache read/write ratio alerting the project brief mandated. It reads
the same tally the row is written from and says whether the run should finalize `failed`,
which is what makes `run_daily` abort and ping the healthchecks.io deadman `/fail` — the
pipeline's only alerting channel, since Sentry is initialized in `main.py` alone. Pure, and
separate from the row: a health write that failed must not also disarm the guard.
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

# The hard-breach constants (NEU-1002, design spec §5.3). `config.Settings` carries the same
# two values so they can be moved without a deploy; a test pins the pair together, exactly as
# it does for T and K — `config` cannot import this package, `retrieval/index.py` reading
# settings would make it a cycle.
#
# **The threshold is a collapse detector, not a drift detector.** At T=0.5/K=25 the measured
# zero-candidate rate is 5.4% over 98,662 production stories, and the catalog-growth curve
# has it falling further as the catalog expands (spec §5.2) — so 25% is roughly 4.6x the
# observed rate and safely under the 32.6% a mis-set T=0.6 would produce. It is deliberately
# not tighter: `run_daily` is fail-fast, so a false breach publishes no summaries at all that
# day, and ordinary drift has the soft tier on `/admin/runs` to be seen in. What this catches
# is the failure ADR-0010 names — an index that built empty or a normalization regression
# silently rejecting the backlog, in a lossy stage, permanently.
MAX_ZERO_CANDIDATE_RATE = 0.25

# The minimum denominator, mirroring `total_failure_error`'s refusal to let a thin backlog
# fail the chain daily. A rate over a handful of stories is noise: a quiet day whose four
# stories all miss is indistinguishable from a collapse, and treating it as one would abort
# the daily chain for a news lull. 50 sits far below an ordinary run's pending set (~2,300
# stories a day are retained) and far above the level where one story moves the rate.
MIN_STORIES_FOR_BREACH = 50


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

    @property
    def zero_candidate_rate(self) -> float | None:
        """The share of stories no film was retrieved for, or None with no denominator.

        None for the same reason `mean_candidates` is: a run with nothing pending has no
        rate, and neither 0.0 ("perfectly healthy") nor 1.0 ("total collapse") is a
        defensible stand-in for one."""
        if not self.stories_retrieved:
            return None
        return self.zero_candidate_stories / self.stories_retrieved


def hard_breach_error(
    tally: RetrievalTally,
    *,
    max_zero_candidate_rate: float = MAX_ZERO_CANDIDATE_RATE,
    min_stories: int = MIN_STORIES_FOR_BREACH,
) -> str | None:
    """Describe the run's retrieval-health breach, or None when there is none.

    Pure, and shaped like `total_failure_error` on purpose — both return a message a pipeline
    joins into the run's `error` — but deliberately *not* folded into it. `StageCounts` asks
    "did the stage produce nothing at all", a rule with no denominator and no rate; this asks
    whether a stage that produced plenty produced the wrong *kind* of nothing. Failing a run
    on a rate is the departure ADR-0010 accepted, and it is scoped here so `total_failure`
    keeps guarding model availability on its own narrow terms (spec §8).

    Two ways not to fire, both load-bearing. Below `min_stories` the rate is noise and is
    never read — the minimum-denominator rule. And `max_zero_candidate_rate` is the highest
    *acceptable* rate rather than the first breaching one, so 1.0 disarms the guard outright,
    which is what makes it retunable from env in an incident rather than by a deploy."""
    rate = tally.zero_candidate_rate
    if rate is None or tally.stories_retrieved < min_stories:
        return None
    if rate <= max_zero_candidate_rate:
        return None
    return (
        f"retrieval health breach: {tally.zero_candidate_stories} of "
        f"{tally.stories_retrieved} stories had no candidates "
        f"({rate:.1%}, over the {max_zero_candidate_rate:.1%} ceiling)"
    )


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
