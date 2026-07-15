"""Shared core for retroactive source cleanups: reject a set of stories and repair the
events they belonged to. Used by both `blocked_cleanup` (admin-blocked domains) and
`google_cleanup` (paused Google News sources) so the event-repair rules live in one place.

Repair rules: an event whose sources were *all* rejected is deleted (its `event_story` and
`event_summary` cascade); a mixed event keeps its surviving sources and has its `event_summary`
deleted so synthesize regenerates a fresh summary next run (its `confidence` is left unchanged
— a survivor still has a non-rejected source). Pure DB I/O — the caller owns the commit.
Nothing is written when `apply=False`; the returned report reflects what *would* change.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.news.models import Event, EventStory, EventSummary, Story


@dataclass
class EventRepairReport:
    stories_rejected: int
    events_deleted: int
    events_resummarized: int


async def reject_stories_and_repair_events(
    session: AsyncSession,
    reject_stories: list[Story],
    *,
    link_note: str,
    apply: bool,
) -> EventRepairReport:
    """Reject `reject_stories` (flip to `rejected`, detach from film, stamp `link_note`) and
    repair every event they were attached to. `reject_stories` may include `pending` rows that
    are in no event yet — those simply get their status flipped."""
    reject_sids = {st.id for st in reject_stories}
    links = (
        list(
            (await session.execute(select(EventStory).where(EventStory.story_id.in_(reject_sids))))
            .scalars()
            .all()
        )
        if reject_sids
        else []
    )
    rejected_per_event: dict[UUID, int] = {}
    for link in links:
        rejected_per_event[link.event_id] = rejected_per_event.get(link.event_id, 0) + 1

    total_per_event: dict[UUID, int] = {}
    if rejected_per_event:
        total_per_event = {
            eid: count
            for eid, count in (
                await session.execute(
                    select(EventStory.event_id, func.count())
                    .where(EventStory.event_id.in_(rejected_per_event))
                    .group_by(EventStory.event_id)
                )
            ).all()
        }
    events_to_delete = {
        eid
        for eid, rejected in rejected_per_event.items()
        if total_per_event.get(eid, 0) == rejected
    }
    events_to_resummarize = set(rejected_per_event) - events_to_delete

    report = EventRepairReport(
        stories_rejected=len(reject_sids),
        events_deleted=len(events_to_delete),
        events_resummarized=len(events_to_resummarize),
    )
    if not apply:
        return report

    now = datetime.now(UTC)
    for link in links:
        await session.delete(link)
    for st in reject_stories:
        st.link_status = "rejected"
        st.film_id = None
        st.link_confidence = None
        st.link_note = link_note
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
