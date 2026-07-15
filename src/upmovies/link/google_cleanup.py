"""One-time cleanup of the Google-News backlog after NEU-717 paused Google ingestion.

Turning Google off stops *new* Google stories, but the DB still holds ones ingested earlier —
`linked` (already in events) and `pending` (not yet clustered, but the next link run would
cluster them and re-fill the site). This rejects both, stamping `link_note = "google-paused"`,
and repairs their events via the shared `reject_stories_and_repair_events` core.

Selected by `story.source` prefix (every Google label starts with `GOOGLE_SOURCE_PREFIX`), the
SQL mirror of `is_google_source`. Pure DB I/O — the caller owns the commit; `apply=False` is a
dry run that changes nothing.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.link.event_repair import reject_stories_and_repair_events
from upmovies.news.feeds import GOOGLE_SOURCE_PREFIX
from upmovies.news.models import Story


@dataclass
class GoogleCleanupReport:
    stories_rejected: int
    events_deleted: int
    events_resummarized: int


async def cleanup_google_sources(session: AsyncSession, *, apply: bool) -> GoogleCleanupReport:
    stories = list(
        (
            await session.execute(
                select(Story).where(
                    Story.source.like(f"{GOOGLE_SOURCE_PREFIX}%"),
                    Story.link_status.in_(("pending", "linked")),
                )
            )
        )
        .scalars()
        .all()
    )
    core = await reject_stories_and_repair_events(
        session, stories, link_note="google-paused", apply=apply
    )
    return GoogleCleanupReport(
        stories_rejected=core.stories_rejected,
        events_deleted=core.events_deleted,
        events_resummarized=core.events_resummarized,
    )
