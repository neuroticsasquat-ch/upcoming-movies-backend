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
# **The threshold is a collapse detector, not a drift detector.** It has to clear the observed
# rate by a wide margin *and* sit below what a mis-set T would produce — the failure ADR-0010
# names, an index that built empty or a normalization regression silently rejecting the
# backlog, in a lossy stage, permanently. Drift is the soft tier's job, below.
#
# **Retuned 0.25 → 0.10 at NEU-1088** (spec §5.13), because the second half of that pair had
# gone slack. Zero-candidate *falls* as the catalog grows, so a mis-set T's rate falls with
# it: T=0.6 produced 32.6% when 0.25 was set and produces 25.6% now, leaving the old ceiling
# clearing it by 0.6pp — inside ordinary run-to-run movement. At T=0.5 the observed rate is
# 0.5%, so 0.10 is ~17x the observed rate and ~2.6x below the rate it must catch, where 0.25
# had an enormous margin on the half that does not matter and almost none on the half that
# does. Still deliberately loose: `run_daily` is fail-fast, so a false breach publishes no
# summaries at all that day.
MAX_ZERO_CANDIDATE_RATE = 0.10

# The soft tier's constant (NEU-1088 §3.6, ADR-0010). Mirrored in `config.Settings` and pinned
# by the same test as the pair above.
#
# **This one warns; it does not fail the run.** Saturation is drift — the signal that says
# *retune*, not *outage* — and `run_daily` is fail-fast, so a hard tier here would publish no
# summaries at all on a day when nothing was actually broken.
#
# Calibrated at K=35 against the 21-day grid (NEU-1088), where saturation measures **1.8%**,
# and 5% was set as roughly 2.8x that — high enough to absorb ordinary run-to-run movement,
# low enough that the next expansion does not pass under it.
#
# **The first production run at K=35 came in at 2.79%** (2026-08-12 01:48 UTC, n=215, over
# the directors-tranche catalog and before the writers tranche admitted anything). So the
# margin is really about **1.8x**, not 2.8x: the grid runs optimistic on saturation, by half
# again on the one comparison available. It is not optimistic everywhere — mean candidates
# came in at 12.89 against the 12.77 it predicted, and at K=25 it read 7.6% against the 7.89%
# the 2026-08-11 run recorded, both close. Saturation is the figure to distrust it on, which
# makes sense: it is the tail of the distribution, and a 16-hour story slice samples that tail
# differently than 21 days of traffic.
#
# The cast tranche (NEU-1090) roughly doubles the catalog again and is expected to breach
# this, which is the intent: breaching it is what schedules the third tuning pass rather than
# leaving it to be rediscovered by hand. On the observed 2.79% that projects to ~5.6% — still
# a breach, but a narrower one than the grid implied, so do not read a near-miss as "the cast
# tranche was cheaper than expected".
#
# **Provisional, and labelled so deliberately.** Two production runs and one offline grid,
# only one of those runs at the live K. Expect the third pass to move it.
SATURATION_WARN_RATE = 0.05

# The minimum denominator, mirroring `total_failure_error`'s refusal to let a thin backlog
# fail the chain daily. A rate over a handful of stories is noise: a quiet day whose four
# stories all miss is indistinguishable from a collapse, and treating it as one would abort
# the daily chain for a news lull. 50 sits far below an ordinary run's pending set (~2,300
# stories a day are retained) and far above the level where one story moves the rate. Read by
# both tiers: a soft tier that cries drift on a quiet day's four stories is one nobody believes
# on the day the drift is real.
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
    def saturation_rate(self) -> float | None:
        """The share of stories the cap truncated, or None with no denominator.

        The soft tier's input, and the number `saturated_stories` alone cannot give: the
        count rises with traffic volume, and it is the *rate* that says whether K still fits
        the catalog."""
        if not self.stories_retrieved:
            return None
        return self.saturated_stories / self.stories_retrieved

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


def soft_breach_note(
    tally: RetrievalTally,
    *,
    warn_rate: float = SATURATION_WARN_RATE,
    min_stories: int = MIN_STORIES_FOR_BREACH,
) -> str | None:
    """Describe the run's cap-saturation drift, or None when there is none.

    Shaped like `hard_breach_error` and deliberately *not* one. That function's return value
    is joined into the run's `error`, and anything landing there finalizes the run `failed`;
    this one returns a clause for the run's **detail** line, which is durable on `/admin/runs`
    and costs nothing when it is wrong. The difference is ADR-0010's doctrine: the hard tier
    catches collapse, the soft tier catches drift, and rising saturation is drift by
    definition — the signal that says *retune*, not *outage*. `run_daily` being fail-fast is
    what makes that distinction expensive to get wrong in the other direction.

    Silent when healthy, rather than reassuring. The detail line is read at a glance, and a
    clause present on every run is one the eye stops seeing.

    The same two escapes as the hard tier, for the same reasons: below `min_stories` the rate
    is noise, and `warn_rate` is the highest *acceptable* rate rather than the first warning
    one, so 1.0 switches the tier off from env rather than by a deploy."""
    rate = tally.saturation_rate
    if rate is None or tally.stories_retrieved < min_stories:
        return None
    if rate <= warn_rate:
        return None
    return (
        f"cap saturation {tally.saturated_stories}/{tally.stories_retrieved} "
        f"({rate:.1%}, over the {warn_rate:.1%} warn rate)"
    )


async def record_retrieval_health(
    session_factory: SessionFactory,
    *,
    run_id: UUID,
    tally: RetrievalTally,
    soft_breach: bool = False,
) -> None:
    """Write the run's aggregate row. Logs and returns on failure; never raises.

    Written once at the end of the link stage rather than incremented per batch: the row is
    a whole-run rate, and a partial one would understate the denominator it is read against.
    Written even when the run had nothing pending, so that a *missing* row keeps its own
    meaning — retrieval did not run at all.

    `soft_breach` is passed in rather than recomputed from `tally` here. The caller already
    holds the clause it puts on the run's detail line, and deriving the flag a second time
    would let the row and that line disagree about the same run — the threshold is a setting,
    so "recompute with the defaults" is not the same question."""
    try:
        async with _owned_session(session_factory) as s:
            s.add(
                RunRetrievalHealth(
                    run_id=run_id,
                    stories_retrieved=tally.stories_retrieved,
                    zero_candidate_stories=tally.zero_candidate_stories,
                    saturated_stories=tally.saturated_stories,
                    mean_candidates=tally.mean_candidates,
                    soft_breach=soft_breach,
                )
            )
            await s.commit()
        log.info(
            "retrieval health: stories=%d zero_candidate=%d saturated=%d mean_candidates=%s "
            "soft_breach=%s",
            tally.stories_retrieved,
            tally.zero_candidate_stories,
            tally.saturated_stories,
            tally.mean_candidates,
            soft_breach,
        )
    except Exception:
        log.exception("recording retrieval health failed")
