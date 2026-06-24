"""The `link` pipeline (Stage 1 → Stage 2): select recent pending stories and link them
in batches, then cluster each film's unclustered linked stories into events. Idempotent —
only touches `pending` stories inside the recency window; re-runs with nothing pending are
a no-op. One batch's failure never rolls back others."""

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.runs import finalize_run, record_llm_usage, record_progress
from upmovies.link.cluster import (
    ClusterPlan,
    apply_cluster_decisions,
    build_cluster_batch_request,
    cluster_film_events,
)
from upmovies.link.linker import (
    BatchCompleter,
    Completer,
    LinkClient,
    apply_link_decisions,
    build_batch_request,
    link_story_batch,
)
from upmovies.link.roster import Roster, build_roster
from upmovies.llm.client import BatchRequest, Usage
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
) -> tuple[int, int, Usage]:
    linked = rejected = 0
    total_usage = Usage()
    for batch_ids in _chunks(pending_ids, batch_size):
        try:
            async with _owned_session(session_factory) as s:
                stories = (
                    (await s.execute(select(Story).where(Story.id.in_(batch_ids)))).scalars().all()
                )
                batch, usage = await link_story_batch(
                    client=client, model=model, roster=roster, stories=list(stories), floor=floor
                )
                await record_progress(s, run_id, processed_delta=batch.linked + batch.rejected)
                await s.commit()
            linked += batch.linked
            rejected += batch.rejected
            total_usage += usage
        except Exception:
            log.exception("link batch of %d stories failed", len(batch_ids))
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=len(batch_ids))
                await s.commit()
    return linked, rejected, total_usage


async def _link_stage_batched(
    *,
    session_factory: SessionFactory,
    client: BatchCompleter,
    run_id: UUID,
    model: str,
    roster: Roster,
    pending_ids: Sequence[UUID],
    batch_size: int,
    floor: float,
) -> tuple[int, int, Usage]:
    chunks = _chunks(pending_ids, batch_size)

    # Build phase: one read-only session; ORM rows are dropped once serialized into requests,
    # so no session is held open across the (possibly long) batch poll.
    requests = []
    async with _owned_session(session_factory) as s:
        for i, batch_ids in enumerate(chunks):
            stories = (
                (await s.execute(select(Story).where(Story.id.in_(batch_ids)))).scalars().all()
            )
            requests.append(
                build_batch_request(
                    custom_id=str(i), model=model, roster=roster, stories=list(stories)
                )
            )

    if not requests:
        return 0, 0, Usage()

    try:
        results = await client.complete_batch(requests)
    except Exception:
        # Whole-batch submit/poll failed: count all chunks failed, leave stories pending.
        log.exception("link batch submit of %d stories failed", len(pending_ids))
        async with _owned_session(session_factory) as s:
            await record_progress(s, run_id, failed_delta=len(pending_ids))
            await s.commit()
        return 0, 0, Usage()

    linked = rejected = 0
    total_usage = Usage()
    for i, batch_ids in enumerate(chunks):
        result = results.get(str(i))
        try:
            if result is None or not result.ok:
                detail = result.error_type if result else "missing"
                raise RuntimeError(f"batch chunk {i} unavailable: {detail}")
            async with _owned_session(session_factory) as s:
                # Re-query: build-phase session closed before polling; fresh ORM objects required.
                stories = (
                    (await s.execute(select(Story).where(Story.id.in_(batch_ids)))).scalars().all()
                )
                # apply_link_decisions calls json.loads; this try/except owns failure isolation.
                applied = apply_link_decisions(
                    raw=result.text, stories=list(stories), roster=roster, floor=floor
                )
                await record_progress(s, run_id, processed_delta=applied.linked + applied.rejected)
                await s.commit()
            linked += applied.linked
            rejected += applied.rejected
            if result.usage is not None:
                total_usage += result.usage
        except Exception:
            log.exception("link batch chunk %d of %d stories failed", i, len(batch_ids))
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=len(batch_ids))
                await s.commit()

    return linked, rejected, total_usage


async def _cluster_stage_sequential(
    *,
    session_factory: SessionFactory,
    client: Completer,
    run_id: UUID,
    model: str,
    film_ids: Sequence[UUID],
    attach_limit: int,
    cluster_max_tokens: int,
) -> tuple[int, int, int, Usage]:
    events_created = stories_clustered = stories_rejected = 0
    total_usage = Usage()
    for film_id in film_ids:
        try:
            async with _owned_session(session_factory) as s:
                cluster, usage = await cluster_film_events(
                    s,
                    client=client,
                    model=model,
                    film_id=film_id,
                    attach_limit=attach_limit,
                    max_tokens=cluster_max_tokens,
                )
                await s.commit()
            events_created += cluster.events_created
            stories_clustered += cluster.stories_clustered
            stories_rejected += cluster.stories_rejected
            total_usage += usage
        except Exception:
            log.exception("clustering failed for film %s", film_id)
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
    return events_created, stories_clustered, stories_rejected, total_usage


async def _cluster_stage_batched(
    *,
    session_factory: SessionFactory,
    client: BatchCompleter,
    run_id: UUID,
    model: str,
    film_ids: Sequence[UUID],
    attach_limit: int,
    cluster_max_tokens: int,
) -> tuple[int, int, int, Usage]:
    # Build phase: one read-only session; ORM rows are dropped once serialized, so no session
    # is held open across the batch poll. The per-film plans (tiny UUID lists) are kept.
    requests: list[BatchRequest] = []
    plans: dict[str, ClusterPlan] = {}
    async with _owned_session(session_factory) as s:
        for film_id in film_ids:
            built = await build_cluster_batch_request(
                s,
                custom_id=str(film_id),
                model=model,
                film_id=film_id,
                attach_limit=attach_limit,
                max_tokens=cluster_max_tokens,
            )
            if built is None:
                continue
            request, plan = built
            requests.append(request)
            plans[request.custom_id] = plan

    if not requests:
        return 0, 0, 0, Usage()

    try:
        results = await client.complete_batch(requests)
    except Exception:
        log.exception("cluster batch submit of %d films failed", len(requests))
        async with _owned_session(session_factory) as s:
            await record_progress(s, run_id, failed_delta=len(requests))
            await s.commit()
        return 0, 0, 0, Usage()

    events_created = stories_clustered = stories_rejected = 0
    total_usage = Usage()
    for custom_id, plan in plans.items():
        result = results.get(custom_id)
        try:
            if result is None or not result.ok:
                detail = result.error_type if result else "missing"
                raise RuntimeError(f"cluster film {custom_id} unavailable: {detail}")
            async with _owned_session(session_factory) as s:
                applied = await apply_cluster_decisions(s, plan=plan, raw=result.text)
                await s.commit()
            events_created += applied.events_created
            stories_clustered += applied.stories_clustered
            stories_rejected += applied.stories_rejected
            if result.usage is not None:
                total_usage += result.usage
        except Exception:
            log.exception("clustering failed for film %s", custom_id)
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()

    return events_created, stories_clustered, stories_rejected, total_usage


async def run_link_ingest(
    *,
    session_factory: SessionFactory,
    client: LinkClient,
    run_id: UUID,
    model: str,
    cluster_model: str,
    recency_days: int,
    attach_limit: int = 25,
    batch_size: int,
    floor: float,
    use_batches: bool = False,
    cluster_use_batches: bool = False,
    cluster_max_tokens: int = 4096,
) -> LinkIngestResult:
    async with _owned_session(session_factory) as s:
        roster = await build_roster(s)

    cutoff = datetime.now(UTC) - timedelta(days=recency_days)
    async with _owned_session(session_factory) as s:
        result = await s.execute(
            select(Story.id).where(
                Story.link_status == "pending",
                func.coalesce(Story.published_at, Story.fetched_at) >= cutoff,
            )
        )
        pending_ids = [row[0] for row in result.all()]

    stage = _link_stage_batched if use_batches else _link_stage_sequential
    linked, rejected, link_usage = await stage(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        model=model,
        roster=roster,
        pending_ids=pending_ids,
        batch_size=batch_size,
        floor=floor,
    )
    async with _owned_session(session_factory) as s:
        await record_llm_usage(
            s, run_id, stage="link", model=model, batched=use_batches, usage=link_usage
        )
        await s.commit()

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

    cluster_stage = _cluster_stage_batched if cluster_use_batches else _cluster_stage_sequential
    events_created, stories_clustered, stories_rejected, cluster_usage = await cluster_stage(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        model=cluster_model,
        film_ids=film_ids,
        attach_limit=attach_limit,
        cluster_max_tokens=cluster_max_tokens,
    )
    async with _owned_session(session_factory) as s:
        await record_llm_usage(
            s,
            run_id,
            stage="cluster",
            model=cluster_model,
            batched=use_batches,
            usage=cluster_usage,
        )
        await s.commit()

    async with _owned_session(session_factory) as s:
        await finalize_run(
            s,
            run_id,
            status="succeeded",
            detail=(
                f"linked {linked}, rejected {rejected}; "
                f"{events_created} events from {stories_clustered} stories "
                f"({stories_rejected} stale-stage rejected)"
            ),
        )
        await s.commit()
    return LinkIngestResult(linked, rejected)
