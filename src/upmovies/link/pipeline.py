"""The `link` pipeline (Stage 1 → Stage 2): select recent pending stories and link them
in batches, then cluster each film's unclustered linked stories into events. Idempotent —
only touches `pending` stories inside the recency window; re-runs with nothing pending are
a no-op. One batch's failure never rolls back others."""

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.runs import (
    StageCounts,
    finalize_run,
    record_llm_calls,
    record_llm_usage,
    record_progress,
    total_failure_error,
)
from upmovies.link.cluster import cluster_film_events
from upmovies.link.linker import (
    Completer,
    StoryCandidates,
    link_retrieval_story_batch,
    reject_zero_candidate_stories,
    story_dek,
)
from upmovies.link.retrieval.health import (
    MAX_ZERO_CANDIDATE_RATE,
    MIN_STORIES_FOR_BREACH,
    RetrievalTally,
    hard_breach_error,
    record_retrieval_health,
)
from upmovies.link.retrieval.index import CandidateIndex, build_candidate_index
from upmovies.link.retrieval.select import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_SCORE_THRESHOLD,
    CandidateSet,
    select_candidates,
)
from upmovies.link.source_stage import run_source_quality_stage
from upmovies.llm.client import CallLog, Usage
from upmovies.news.models import EventStory, Story

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


@dataclass
class LinkIngestResult:
    linked: int
    rejected: int


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


def _chunks(seq: Sequence[UUID], size: int) -> list[list[UUID]]:
    return [list(seq[i : i + size]) for i in range(0, len(seq), size)]


async def _link_stage_retrieval(
    *,
    session_factory: SessionFactory,
    client: Completer,
    run_id: UUID,
    model: str,
    index: CandidateIndex,
    pending_ids: Sequence[UUID],
    batch_size: int,
    floor: float,
    run_date: date,
    threshold: float,
    limit: int,
    tally: RetrievalTally,
) -> tuple[int, int, Usage, StageCounts]:
    """The link stage: retrieve per story, reject the ones with no candidates without a
    model call, and classify the rest against their own lists.

    Per-batch failure isolation, with one deliberate wrinkle: the zero-candidate rejects
    **commit before the classifier call** and are counted as committed straight away. No
    model decided them, so no model failure may take them back — which is what makes an
    outage's tally `0 processed / N failed` rather than a partial success (ADR-0009), and
    what the outage test in `test_pipeline_retrieval.py` pins."""
    linked = classified_rejected = zero_candidate_rejected = failed_stories = 0
    total_usage = Usage()
    for batch_ids in _chunks(pending_ids, batch_size):
        # Owned outside the try so a batch that fails *after* its call still records what
        # that call cost — both as an `llm_call` row and in the stage aggregate (NEU-975).
        calls = CallLog()
        # What is still at risk if this batch fails, decremented as work commits.
        at_risk = len(batch_ids)
        try:
            async with _owned_session(session_factory) as s:
                stories = (
                    (await s.execute(select(Story).where(Story.id.in_(batch_ids)))).scalars().all()
                )
                batch = [
                    StoryCandidates(
                        story=story,
                        candidates=_retrieve(index, story, threshold=threshold, limit=limit),
                    )
                    for story in stories
                ]
                for entry in batch:
                    tally.add(entry.candidates)

                no_candidates = [e.story for e in batch if e.candidates.is_empty]
                to_classify = [e for e in batch if not e.candidates.is_empty]
                if no_candidates:
                    rejected_here = reject_zero_candidate_stories(no_candidates)
                    # Counted into the *run row* — the stage really did dispose of these
                    # stories — but pointedly not into `StageCounts` below.
                    await record_progress(s, run_id, processed_delta=rejected_here)
                    await s.commit()
                    zero_candidate_rejected += rejected_here
                    at_risk -= rejected_here

                result = await link_retrieval_story_batch(
                    client=client,
                    model=model,
                    batch=to_classify,
                    floor=floor,
                    run_date=run_date,
                    calls=calls,
                )
                if to_classify:
                    await record_progress(
                        s, run_id, processed_delta=result.linked + result.rejected
                    )
                    await s.commit()
            linked += result.linked
            classified_rejected += result.rejected
        except Exception:
            # `at_risk`, not the batch size: the zero-candidate rejects in this batch have
            # already committed, and logging them as failed would misreport the outage the
            # counters below describe correctly.
            log.exception("link batch of %d stories failed", at_risk)
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=at_risk)
                await s.commit()
            failed_stories += at_risk
        finally:
            total_usage += calls.usage
            if calls.results:
                async with _owned_session(session_factory) as s:
                    await record_llm_calls(
                        s, run_id, stage="link", model=model, results=calls.results
                    )
                    await s.commit()
    return (
        linked,
        classified_rejected + zero_candidate_rejected,
        total_usage,
        # **`processed` is classifier output only.** `total_failure` returns False the
        # instant `processed > 0`, so folding the zero-candidate rejects in here would let a
        # total Anthropic outage report ~30% processed, finalize the run `succeeded`, and let
        # the fail-fast daily chain proceed — while the stories the outage cost aged out of
        # the recency window unrecoverably, `link` being the repo's one lossy stage. The
        # rejects feed retrieval health instead, via `tally` (ADR-0009, NEU-999).
        StageCounts(processed=linked + classified_rejected, failed=failed_stories),
    )


def _retrieve(index: CandidateIndex, story: Story, *, threshold: float, limit: int) -> CandidateSet:
    """One story's candidate set, scored on the headline + dek the classifier is shown."""
    return select_candidates(
        index,
        headline=story.title,
        dek=story_dek(story),
        threshold=threshold,
        limit=limit,
    )


async def _cluster_stage_sequential(
    *,
    session_factory: SessionFactory,
    client: Completer,
    run_id: UUID,
    model: str,
    film_ids: Sequence[UUID],
    attach_limit: int,
    cluster_max_tokens: int,
    unresolved_tier: str = "acceptable",
    dedup_days: int = 14,
    release_change_window_days: int = 14,
    run_date: date,
) -> tuple[int, int, int, Usage, StageCounts]:
    events_created = stories_clustered = stories_rejected = 0
    films_ok = films_failed = 0
    total_usage = Usage()
    for film_id in film_ids:
        calls = CallLog()  # see `_link_stage_retrieval` — owned outside the try on purpose
        try:
            async with _owned_session(session_factory) as s:
                cluster = await cluster_film_events(
                    s,
                    client=client,
                    model=model,
                    film_id=film_id,
                    attach_limit=attach_limit,
                    max_tokens=cluster_max_tokens,
                    unresolved_tier=unresolved_tier,
                    dedup_days=dedup_days,
                    release_change_window_days=release_change_window_days,
                    run_date=run_date,
                    calls=calls,
                )
                # One film, counted the same way the catch-block counts a failure, so a clean
                # cluster stage no longer persists as 0 processed / 0 failed — which on the
                # run row was indistinguishable from a stage that never ran (NEU-987). These
                # counters remain a whole-run total across both stages, so they still cannot
                # reproduce the per-stage decision; the guard reads the in-memory counts.
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            events_created += cluster.events_created
            stories_clustered += cluster.stories_clustered
            stories_rejected += cluster.stories_rejected
            films_ok += 1
        except Exception:
            log.exception("clustering failed for film %s", film_id)
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            films_failed += 1
        finally:
            total_usage += calls.usage
            if calls.results:
                async with _owned_session(session_factory) as s:
                    await record_llm_calls(
                        s, run_id, stage="cluster", model=model, results=calls.results
                    )
                    await s.commit()
    return (
        events_created,
        stories_clustered,
        stories_rejected,
        total_usage,
        # Counted in films, not events: a film can cluster fine and yield no new event. What a
        # lone failure means here is settled once by STAGE_KINDS["cluster"] (NEU-987/NEU-988):
        # an unclustered film is re-selected every run until it clusters.
        StageCounts(processed=films_ok, failed=films_failed),
    )


async def run_link_ingest(
    *,
    session_factory: SessionFactory,
    client: Completer,
    run_id: UUID,
    model: str,
    cluster_model: str,
    recency_days: int,
    attach_limit: int = 25,
    batch_size: int,
    floor: float,
    cluster_max_tokens: int = 4096,
    unresolved_tier: str = "acceptable",
    dedup_days: int = 14,
    release_change_window_days: int = 14,
    source_gate_enabled: bool = False,
    source_judge_model: str = "claude-haiku-4-5",
    retrieval_threshold: float = DEFAULT_SCORE_THRESHOLD,
    retrieval_max_candidates: int = DEFAULT_CANDIDATE_LIMIT,
    retrieval_max_zero_candidate_rate: float = MAX_ZERO_CANDIDATE_RATE,
    retrieval_health_min_stories: int = MIN_STORIES_FOR_BREACH,
) -> LinkIngestResult:
    run_date = datetime.now(UTC).date()
    cutoff = datetime.now(UTC) - timedelta(days=recency_days)
    async with _owned_session(session_factory) as s:
        result = await s.execute(
            select(Story.id).where(
                Story.link_status == "pending",
                func.coalesce(Story.published_at, Story.fetched_at) >= cutoff,
            )
        )
        pending_ids = [row[0] for row in result.all()]

    # The link path owns the catalog read it needs. **No roster is built at all** — deleting
    # that ~50k-token prefix was the project's whole point (spec §1) — and a failed index
    # build is deliberately *not* caught: with no roster left to fall back to (NEU-1004,
    # spec §5.5), an unbuildable index would zero-candidate the whole backlog and reject it,
    # so the run must crash and finalize `failed` instead.
    async with _owned_session(session_factory) as s:
        index = await build_candidate_index(s)
    tally = RetrievalTally()
    linked, rejected, link_usage, link_counts = await _link_stage_retrieval(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        model=model,
        index=index,
        pending_ids=pending_ids,
        batch_size=batch_size,
        floor=floor,
        run_date=run_date,
        threshold=retrieval_threshold,
        limit=retrieval_max_candidates,
        tally=tally,
    )
    # Immediately after the stage that produced the numbers: a *missing* health row is how
    # "retrieval did not run at all" is told apart from a run whose retrieval found nothing.
    await record_retrieval_health(session_factory, run_id=run_id, tally=tally)
    # Read off the tally rather than the row just written: that write is best-effort by
    # contract, and an outage that swallowed it must not also disarm the guard.
    retrieval_breach = hard_breach_error(
        tally,
        max_zero_candidate_rate=retrieval_max_zero_candidate_rate,
        min_stories=retrieval_health_min_stories,
    )
    if retrieval_breach:
        log.error("%s", retrieval_breach)
    async with _owned_session(session_factory) as s:
        await record_llm_usage(s, run_id, stage="link", model=model, usage=link_usage)
        await s.commit()

    # --- Source-quality sub-stage: resolve domains, judge unknowns, drop admin-blocked ---
    if source_gate_enabled:
        source_result, source_usage = await run_source_quality_stage(
            session_factory=session_factory,
            client=client,
            run_id=run_id,
            judge_model=source_judge_model,
            unresolved_tier=unresolved_tier,
        )
        async with _owned_session(session_factory) as s:
            await record_llm_usage(
                s, run_id, stage="source_judge", model=source_judge_model, usage=source_usage
            )
            await s.commit()
        log.info(
            "source-gate: resolved %d, judged %d, blocked %d",
            source_result.resolved,
            source_result.judged,
            source_result.blocked,
        )

    # --- Stage 2: cluster + classify, per film with unclustered linked stories ---
    async with _owned_session(session_factory) as s:
        clustered = exists().where(EventStory.story_id == Story.id)
        film_ids = [
            fid
            for fid in (
                await s.execute(
                    select(Story.film_id)
                    .where(
                        Story.link_status == "linked",
                        Story.film_id.is_not(None),
                        ~clustered,
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
            if fid is not None
        ]

    (
        events_created,
        stories_clustered,
        stories_rejected,
        cluster_usage,
        cluster_counts,
    ) = await _cluster_stage_sequential(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        model=cluster_model,
        film_ids=film_ids,
        attach_limit=attach_limit,
        cluster_max_tokens=cluster_max_tokens,
        unresolved_tier=unresolved_tier,
        dedup_days=dedup_days,
        release_change_window_days=release_change_window_days,
        run_date=run_date,
    )
    async with _owned_session(session_factory) as s:
        await record_llm_usage(s, run_id, stage="cluster", model=cluster_model, usage=cluster_usage)
        await s.commit()

    # Two independent guards, joined only here. `total_failure_error` watches model
    # availability on its narrow "produced nothing at all" rule; the breach watches the rate
    # at which retrieval disposes of stories no model ever sees (ADR-0010). A run can trip
    # both — an outage on a broken index does — and the error names each one that fired.
    error = (
        "; ".join(
            filter(
                None,
                (total_failure_error(link=link_counts, cluster=cluster_counts), retrieval_breach),
            )
        )
        or None
    )
    async with _owned_session(session_factory) as s:
        await finalize_run(
            s,
            run_id,
            status="failed" if error else "succeeded",
            error=error,
            detail=(
                f"linked {linked}, rejected {rejected}; "
                f"{events_created} events from {stories_clustered} stories "
                f"({stories_rejected} stale-stage rejected)"
            ),
        )
        await s.commit()
    return LinkIngestResult(linked, rejected)
