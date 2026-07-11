"""Admin moderation of story↔film links: reverse a bad link decision. Reject the affected
stories and detach them from their event so neither the link stage (touches only `pending`)
nor the cluster stage (touches only `linked` + unclustered) reprocesses them; drop events that
become empty, and delete the `event_summary` row on surviving events so synthesize regenerates
a fresh summary next run (bumping `updated_at` alone no longer triggers reselection — summaries
are write-once).

Pure DB I/O — the caller owns the commit."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.news.models import Event, EventStory, EventSummary, Story


class EventNotFound(Exception):
    """No event with the given id (or no summary row for it)."""


class StoryNotInEvent(Exception):
    """No story with the given url is attached to the given event."""


class SummaryNotEdited(Exception):
    """The event's summary is machine-generated (edited_at IS NULL); reset does not apply."""


@dataclass
class DelinkResult:
    delinked: int
    event_removed: bool
    resummarize_queued: bool


def _reject(story: Story, now: datetime) -> None:
    story.link_status = "rejected"
    story.film_id = None
    story.link_confidence = None
    story.link_note = "manual-unlink"
    story.linked_at = now


async def _remaining_sources(session: AsyncSession, event_id: UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(EventStory).where(EventStory.event_id == event_id)
        )
    ).scalar_one()


async def delink_story(session: AsyncSession, *, event_id: UUID, url: str) -> DelinkResult:
    event = await session.get(Event, event_id)
    if event is None:
        raise EventNotFound(str(event_id))
    story = (await session.execute(select(Story).where(Story.url == url))).scalar_one_or_none()
    if story is None:
        raise StoryNotInEvent(url)
    link = (
        await session.execute(
            select(EventStory).where(
                EventStory.event_id == event_id, EventStory.story_id == story.id
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise StoryNotInEvent(url)

    now = datetime.now(UTC)
    _reject(story, now)
    await session.delete(link)
    await session.flush()

    if await _remaining_sources(session, event_id) == 0:
        await session.delete(event)  # event_summary cascades
        return DelinkResult(delinked=1, event_removed=True, resummarize_queued=False)
    event.updated_at = now
    summary = await session.get(EventSummary, event_id)
    if summary is not None:
        await session.delete(summary)
    return DelinkResult(delinked=1, event_removed=False, resummarize_queued=True)


async def edit_summary(
    session: AsyncSession, *, event_id: UUID, summary: str, user_id: UUID
) -> EventSummary:
    """Overwrite an event's summary with admin-authored text and stamp the human-edit marker
    (edited_at/edited_by). The summary row must already exist — synthesize creates it, and an
    event only reaches the public surface once it does. Returns the updated row so the caller
    can build its response. Caller owns the commit."""
    row = await session.get(EventSummary, event_id)
    if row is None:
        raise EventNotFound(str(event_id))
    row.summary = summary
    row.edited_at = datetime.now(UTC)
    row.edited_by = user_id
    await session.flush()
    return row


async def reset_summary(session: AsyncSession, *, event_id: UUID) -> None:
    """Reset a human-edited summary back to AI by deleting the row: under write-once the event
    then has no summary, so the next synthesize run re-summarizes it fresh with the current
    prompt. Only applies to edited summaries (edited_at IS NOT NULL). Caller owns the commit."""
    row = await session.get(EventSummary, event_id)
    if row is None:
        raise EventNotFound(str(event_id))
    if row.edited_at is None:
        raise SummaryNotEdited(str(event_id))
    await session.delete(row)


async def delete_event(session: AsyncSession, *, event_id: UUID) -> DelinkResult:
    event = await session.get(Event, event_id)
    if event is None:
        raise EventNotFound(str(event_id))
    story_ids = (
        (await session.execute(select(EventStory.story_id).where(EventStory.event_id == event_id)))
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    stories = (await session.execute(select(Story).where(Story.id.in_(story_ids)))).scalars().all()
    for story in stories:
        _reject(story, now)
    await session.flush()
    await session.delete(event)  # event_story + event_summary cascade
    return DelinkResult(delinked=len(stories), event_removed=True, resummarize_queued=False)
