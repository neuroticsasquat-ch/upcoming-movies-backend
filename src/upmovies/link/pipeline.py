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

from upmovies.ingest.runs import finalize_run, record_progress
from upmovies.link.cluster import cluster_film_events
from upmovies.link.linker import Completer, link_story_batch
from upmovies.link.roster import build_roster
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


async def run_link_ingest(
    *,
    session_factory: SessionFactory,
    client: Completer,
    run_id: UUID,
    model: str,
    cluster_model: str,
    recency_days: int,
    batch_size: int,
    floor: float,
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

    linked = rejected = 0
    for batch_ids in _chunks(pending_ids, batch_size):
        try:
            async with _owned_session(session_factory) as s:
                stories = (
                    (await s.execute(select(Story).where(Story.id.in_(batch_ids)))).scalars().all()
                )
                batch = await link_story_batch(
                    client=client, model=model, roster=roster, stories=list(stories), floor=floor
                )
                await record_progress(s, run_id, processed_delta=batch.linked + batch.rejected)
                await s.commit()
            linked += batch.linked
            rejected += batch.rejected
        except Exception:
            log.exception("link batch of %d stories failed", len(batch_ids))
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=len(batch_ids))
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

    events_created = 0
    stories_clustered = 0
    for film_id in film_ids:
        try:
            async with _owned_session(session_factory) as s:
                cluster = await cluster_film_events(
                    s,
                    client=client,
                    model=cluster_model,
                    film_id=film_id,
                    recency_days=recency_days,
                )
                await s.commit()
            events_created += cluster.events_created
            stories_clustered += cluster.stories_clustered
        except Exception:
            log.exception("clustering failed for film %s", film_id)
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()

    async with _owned_session(session_factory) as s:
        await finalize_run(
            s,
            run_id,
            status="succeeded",
            detail=(
                f"linked {linked}, rejected {rejected}; "
                f"{events_created} events from {stories_clustered} stories"
            ),
        )
        await s.commit()
    return LinkIngestResult(linked, rejected)
