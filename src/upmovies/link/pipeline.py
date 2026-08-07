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

from upmovies.config import LinkRetrievalMode
from upmovies.ingest.runs import (
    StageCounts,
    finalize_run,
    record_llm_calls,
    record_llm_usage,
    record_progress,
    total_failure_error,
)
from upmovies.link.cluster import cluster_film_events
from upmovies.link.linker import Completer, link_story_batch
from upmovies.link.retrieval.select import DEFAULT_CANDIDATE_LIMIT, DEFAULT_SCORE_THRESHOLD
from upmovies.link.roster import Roster, build_roster
from upmovies.link.shadow import ShadowObserver, build_shadow_observer
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


async def _link_stage_sequential(
    *,
    session_factory: SessionFactory,
    client: Completer,
    run_id: UUID,
    model: str,
    roster: Roster,
    pending_ids: Sequence[UUID],
    batch_size: int,
    floor: float,
    run_date: date,
    shadow: ShadowObserver | None = None,
) -> tuple[int, int, Usage, StageCounts]:
    linked = rejected = failed_stories = 0
    total_usage = Usage()
    for batch_ids in _chunks(pending_ids, batch_size):
        # Owned outside the try so a batch that fails *after* its call still records what that
        # call cost — both as an `llm_call` row and in the stage aggregate (NEU-975).
        calls = CallLog()
        try:
            async with _owned_session(session_factory) as s:
                stories = (
                    (await s.execute(select(Story).where(Story.id.in_(batch_ids)))).scalars().all()
                )
                batch = await link_story_batch(
                    client=client,
                    model=model,
                    roster=roster,
                    stories=list(stories),
                    floor=floor,
                    run_date=run_date,
                    calls=calls,
                )
                await record_progress(s, run_id, processed_delta=batch.linked + batch.rejected)
                await s.commit()
            linked += batch.linked
            rejected += batch.rejected
            if shadow is not None:
                # After the commit, so the picks observed are the ones that stuck — and
                # outside the session block, since the observer owns a session of its own
                # (nesting two would close the outer one). `expire_on_commit=False` is what
                # keeps the decided stories readable out here. Never raises, so a batch
                # cannot fail on shadow work; see `link/shadow.py`.
                await shadow.observe_batch(stories)
        except Exception:
            log.exception("link batch of %d stories failed", len(batch_ids))
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=len(batch_ids))
                await s.commit()
            failed_stories += len(batch_ids)
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
        rejected,
        total_usage,
        StageCounts(processed=linked + rejected, failed=failed_stories),
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
        calls = CallLog()  # see `_link_stage_sequential` — owned outside the try on purpose
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
    retrieval_mode: LinkRetrievalMode = "off",
    retrieval_threshold: float = DEFAULT_SCORE_THRESHOLD,
    retrieval_max_candidates: int = DEFAULT_CANDIDATE_LIMIT,
) -> LinkIngestResult:
    async with _owned_session(session_factory) as s:
        roster = await build_roster(s)
    # Alongside the roster and on the same once-per-run terms: one catalog read, then pure
    # lookups. None unless the mode is `shadow` (or the build failed) — see `link/shadow.py`.
    shadow = await build_shadow_observer(
        session_factory,
        run_id=run_id,
        mode=retrieval_mode,
        threshold=retrieval_threshold,
        limit=retrieval_max_candidates,
    )

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

    linked, rejected, link_usage, link_counts = await _link_stage_sequential(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        model=model,
        roster=roster,
        pending_ids=pending_ids,
        batch_size=batch_size,
        floor=floor,
        run_date=run_date,
        shadow=shadow,
    )
    if shadow is not None:
        # Immediately after the stage that produced the numbers: nothing may sit between
        # them and leave probe rows without the denominator they are read against, since a
        # *missing* health row is how "shadow did not run at all" is told apart.
        await shadow.record_health()
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

    error = total_failure_error(link=link_counts, cluster=cluster_counts)
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
