"""Shadow observation: run candidate retrieval beside the roster path and record what it
*would* have offered, without letting it decide anything.

Retrieval is pure and needs no model call, so the shadow does not have to be an offline
replay against stored rows — it runs **inside the live pipeline**, on real traffic, at true
catalog scale, for no added LLM cost, and it exercises the same code path that later goes
live. That is what makes this the cutover evidence rather than a rehearsal of it.

**Nothing here is allowed to fail a run.** `ShadowObserver`'s two public methods swallow
their own failures and log them: a retrieval bug must degrade to a missing observation, not
a lost story, because the roster path is still the one deciding. Said once here rather than
restated at every call site in `link/pipeline.py`.

**Two grains, for two different questions.** `LinkRetrievalProbe` gets one row per story the
roster *linked* — the only stories with a pick to measure retrieval against, and the reason
this table is shadow-only: once retrieval decides, there is no second opinion to adjudicate
against. `RunRetrievalHealth` gets one row per run, counted over *every* story retrieval ran
over, because the zero-candidate majority (ADR-0009) never reaches the probe table and is
exactly what the denominator is for. That row is written by both paths and so lives in
`link/retrieval/health.py`, which outlives this module.

**Reading the output.** Recall here is measured against roster picks, and the roster makes
false positives — so retrieval declining to surface one is a win that a bare percentage
scores as a loss. The hand-adjudication step in M4 exists for that; the raw rate is not truth.
The hard-breach guard does not read this path (ADR-0010, NEU-1002). It is armed only under
`on`: here the roster still decides, so retrieval offering nothing costs no story its link,
and failing the daily chain over a measurement would be the guard firing at the one time it
has nothing to protect."""

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.config import LinkRetrievalMode
from upmovies.ingest.models import LinkRetrievalProbe
from upmovies.link.linker import story_dek
from upmovies.link.retrieval.health import RetrievalTally, record_retrieval_health
from upmovies.link.retrieval.index import CandidateIndex, build_candidate_index
from upmovies.link.retrieval.select import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_SCORE_THRESHOLD,
    CandidateSet,
    select_candidates,
)
from upmovies.news.models import Story

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@dataclass(frozen=True)
class StoryObservation:
    """Where retrieval put the roster's pick for one story — a `LinkRetrievalProbe` row
    before it is one.

    `rank` and `score` are independent on purpose. A pick with a score but no rank was lost
    to the cap, which raising K fixes; one with neither never cleared the threshold, which K
    cannot reach at all (ADR-0008)."""

    story_id: UUID
    film_id: UUID
    retrieved: bool
    rank: int | None
    score: float | None
    candidate_count: int


def observe_story(story: Story, candidates: CandidateSet) -> StoryObservation | None:
    """What `candidates` says about the roster's pick for `story`, or None when there is
    nothing to adjudicate.

    A story the roster rejected produces no observation: retrieval offering nothing for it
    is agreement, not a disagreement worth a row. Pure — the caller does the retrieving, so
    this stays testable against a hand-built candidate set."""
    if story.link_status != "linked" or story.film_id is None:
        return None
    rank = candidates.rank_of(story.film_id)
    return StoryObservation(
        story_id=story.id,
        film_id=story.film_id,
        retrieved=rank is not None,
        rank=rank,
        # Answered even for a pick the cap discarded — that pairing is the whole point.
        score=candidates.score_of(story.film_id),
        # Post-cap, so it is the set the model would actually have been shown.
        candidate_count=len(candidates.candidates),
    )


class ShadowObserver:
    """One run's shadow pass: the index built once, the tally accumulated across batches.

    Both methods are best-effort by contract (see the module docstring) — they log and
    return rather than raising, so no shadow failure can reach the pipeline's per-batch
    error handling and cost a story its link."""

    def __init__(
        self,
        index: CandidateIndex,
        *,
        session_factory: SessionFactory,
        run_id: UUID,
        threshold: float = DEFAULT_SCORE_THRESHOLD,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> None:
        self._index = index
        self._session_factory = session_factory
        self._run_id = run_id
        self._threshold = threshold
        self._limit = limit
        self._tally = RetrievalTally()

    async def observe_batch(self, stories: Sequence[Story]) -> None:
        """Retrieve for every story in a batch the roster has already decided, count them
        all, and write a probe row for each one it linked.

        Called after the batch commits, so the picks read here are the committed ones. Owns
        its own session for the same reason: a probe write that fails must not roll back the
        links the batch just made.

        Two things the denominator therefore does *not* count, both deliberately. A batch
        whose link call failed is never observed: the roster decided nothing there, so there
        is no pick to measure and retrieval ran over nothing. And a story the source-quality
        sub-stage blocks *after* this point keeps its probe row — the outlet's tier says
        nothing about whether retrieval could reach the film, and the live path will retrieve
        ahead of that gate too, so dropping those would measure a path that does not ship."""
        try:
            observations = [
                observation
                for story in stories
                if (observation := observe_story(story, self._retrieve(story))) is not None
            ]
            if not observations:
                return
            async with _owned_session(self._session_factory) as s:
                s.add_all(
                    LinkRetrievalProbe(
                        run_id=self._run_id,
                        story_id=o.story_id,
                        film_id=o.film_id,
                        retrieved=o.retrieved,
                        rank=o.rank,
                        score=o.score,
                        candidate_count=o.candidate_count,
                    )
                    for o in observations
                )
                await s.commit()
        except Exception:
            log.exception("shadow retrieval failed for a batch of %d stories", len(stories))

    async def record_health(self) -> None:
        """Write the run's aggregate row — the same row and the same best-effort contract
        the live path writes, so a shadow period and the stage that follows it are read on
        one shape (see `link/retrieval/health.py`)."""
        await record_retrieval_health(self._session_factory, run_id=self._run_id, tally=self._tally)

    def _retrieve(self, story: Story) -> CandidateSet:
        """The candidate set for one story, counted into the run's tally as it goes.

        Retrieves on headline + dek, from the same fields the classifier is shown, so the
        measurement is of the text that will actually ship."""
        candidates = select_candidates(
            self._index,
            headline=story.title,
            dek=story_dek(story),
            threshold=self._threshold,
            limit=self._limit,
        )
        self._tally.add(candidates)
        return candidates


async def build_shadow_observer(
    session_factory: SessionFactory,
    *,
    run_id: UUID,
    mode: LinkRetrievalMode,
    threshold: float = DEFAULT_SCORE_THRESHOLD,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> ShadowObserver | None:
    """The observer for this run, or None when retrieval is not running in shadow.

    `on` gets None too, and correctly: under `on` retrieval is not observing the roster, it
    *is* the path (`link/pipeline.py`), and there is no second opinion left for a probe row
    to adjudicate against.

    A failed index build returns None: the whole run then proceeds on the roster path with
    no observations, which is the same degradation every other failure here takes. The live
    path deliberately does **not** degrade that way — with no roster to fall back to, an
    unbuildable index would zero-candidate the entire backlog."""
    if mode != "shadow":
        return None
    try:
        async with _owned_session(session_factory) as s:
            index = await build_candidate_index(s)
    except Exception:
        log.exception("building the shadow retrieval index failed; the run continues without it")
        return None
    return ShadowObserver(
        index, session_factory=session_factory, run_id=run_id, threshold=threshold, limit=limit
    )
