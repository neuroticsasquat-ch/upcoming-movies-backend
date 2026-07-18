"""Retroactive cleanup of stories/events from admin-blocked source domains.

The live source-quality gate (link/source_stage.py) only hard-drops blocked stories that are
still unclustered on the *next* ingest run; a story already clustered into an event before its
domain was blocked is never revisited. This closes that gap: it rejects every linked story
whose effective tier is `blocked` and repairs their events (via the shared
`reject_stories_and_repair_events` core).

Pure DB I/O — the caller owns the commit. Nothing is written when `apply=False`; the returned
report reflects what *would* change.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.link.event_repair import reject_stories_and_repair_events
from upmovies.news.models import Story
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

    blocked: list[Story] = []
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
            blocked.append(st)
            if domain:
                blocked_domains.add(domain)

    core = await reject_stories_and_repair_events(
        session, blocked, link_note="source-blocked", apply=apply
    )
    return BlockedCleanupReport(
        blocked_domains=sorted(blocked_domains),
        stories_rejected=core.stories_rejected,
        events_deleted=core.events_deleted,
        events_resummarized=core.events_resummarized,
    )
