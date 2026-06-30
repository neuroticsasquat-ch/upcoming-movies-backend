"""Admin moderation of story↔film links: reverse a bad link decision. Reject the affected
stories and detach them from their event so neither the link stage (touches only `pending`)
nor the cluster stage (touches only `linked` + unclustered) reprocesses them; drop events that
become empty, and bump `updated_at` on surviving events so synthesize re-summarizes them.

Pure DB I/O — the caller owns the commit."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.news.models import Event, EventStory, Story


class EventNotFound(Exception):
    """No event with the given id."""


class StoryNotInEvent(Exception):
    """No story with the given url is attached to the given event."""


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
    return DelinkResult(delinked=1, event_removed=False, resummarize_queued=True)


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
