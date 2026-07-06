"""Retroactive cleanup of stories/events from admin-blocked source domains.

The live source-quality gate (link/source_stage.py) only hard-drops blocked stories that are
still unclustered on the *next* ingest run; a story already clustered into an event before its
domain was blocked is never revisited. This closes that gap: it rejects every linked story
whose effective tier is `blocked`, detaches it from its event, deletes events left with no
remaining sources, and deletes the `event_summary` row on surviving events so synthesize
regenerates a fresh summary next run (their `confidence` is left unchanged — a survivor still
has a non-blocked source).

Pure DB I/O — the caller owns the commit. Nothing is written when `apply=False`; the returned
report reflects what *would* change.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.news.source_quality import domain_for_story, effective_tier, get_source_domains

log = logging.getLogger(__name__)


@dataclass
class BlockedCleanupReport:
    blocked_domains: list[str]
    stories_rejected: int
    events_deleted: int
    events_resummarized: int


async def cleanup_blocked_sources(
    session: AsyncSession, *, unresolved_tier: str = "acceptable", apply: bool
) -> BlockedCleanupReport:
    stories = list(
        (await session.execute(select(Story).where(Story.link_status == "linked"))).scalars().all()
    )
    domain_by_sid = {
        st.id: domain_for_story(url=st.url, resolved_url=st.resolved_url) for st in stories
    }
    rows = await get_source_domains(session, [d for d in domain_by_sid.values() if d])

    blocked_sids: set[UUID] = set()
    blocked_domains: set[str] = set()
    for st in stories:
        domain = domain_by_sid.get(st.id)
        row = rows.get(domain) if domain else None
        tier = effective_tier(
            llm_tier=row.llm_tier if row else None,
            admin_override=row.admin_override if row else "none",
            unresolved_default=unresolved_tier,
        )
        if tier == "blocked":
            blocked_sids.add(st.id)
            if domain:
                blocked_domains.add(domain)

    links = (
        list(
            (await session.execute(select(EventStory).where(EventStory.story_id.in_(blocked_sids))))
            .scalars()
            .all()
        )
        if blocked_sids
        else []
    )
    blocked_per_event: dict[UUID, int] = {}
    for link in links:
        blocked_per_event[link.event_id] = blocked_per_event.get(link.event_id, 0) + 1

    total_per_event: dict[UUID, int] = {}
    if blocked_per_event:
        total_per_event = {
            eid: count
            for eid, count in (
                await session.execute(
                    select(EventStory.event_id, func.count())
                    .where(EventStory.event_id.in_(blocked_per_event))
                    .group_by(EventStory.event_id)
                )
            ).all()
        }
    # An event whose only sources were blocked is left empty → delete it; a mixed event keeps
    # its surviving sources and has its summary deleted so it gets a fresh one next run.
    events_to_delete = {
        eid for eid, blocked in blocked_per_event.items() if total_per_event.get(eid, 0) == blocked
    }
    events_to_resummarize = set(blocked_per_event) - events_to_delete

    report = BlockedCleanupReport(
        blocked_domains=sorted(blocked_domains),
        stories_rejected=len(blocked_sids),
        events_deleted=len(events_to_delete),
        events_resummarized=len(events_to_resummarize),
    )
    if not apply:
        return report

    now = datetime.now(UTC)
    for link in links:
        await session.delete(link)
    for st in stories:
        if st.id in blocked_sids:
            st.link_status = "rejected"
            st.film_id = None
            st.link_confidence = None
            st.link_note = "source-blocked"
            st.linked_at = now
    await session.flush()
    for eid in events_to_delete:
        event = await session.get(Event, eid)
        if event is not None:
            await session.delete(event)  # event_story + event_summary cascade
    for eid in events_to_resummarize:
        event = await session.get(Event, eid)
        if event is not None:
            event.updated_at = now
        summary = await session.get(EventSummary, eid)
        if summary is not None:
            await session.delete(summary)
    await session.flush()
    return report
