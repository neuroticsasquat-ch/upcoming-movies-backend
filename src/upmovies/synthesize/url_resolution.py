"""URL-resolution stage for the synthesize pipeline. Marks Google News stories on
freshly-synthesized events eligible (`none` -> `pending`), then decodes pending
stories to publisher URLs with capped retry and a per-run cap. Decoupled from
summarization: a failure here never affects committed summaries."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.news.models import EventStory, Story
from upmovies.news.resolve import (
    is_google_news_url,
    resolve_google_news_url,
)

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]
Resolver = Callable[[httpx.AsyncClient, str], Awaitable[str | None]]

MAX_ATTEMPTS = 3
RESOLVE_PER_RUN = 50
POLITENESS_DELAY_SECONDS = 1.0


@dataclass
class ResolveResult:
    resolved: int
    failed: int
    pending: int


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


async def mark_eligible(session: AsyncSession, event_ids: list[UUID]) -> int:
    """Flip Google News stories on the given events from 'none' to 'pending'. Scoped
    to event_ids so events synthesized before this feature stay 'none'. Caller commits."""
    if not event_ids:
        return 0
    rows = (
        (
            await session.execute(
                select(Story)
                .join(EventStory, EventStory.story_id == Story.id)
                .where(EventStory.event_id.in_(event_ids), Story.resolve_state == "none")
            )
        )
        .scalars()
        .all()
    )
    eligible_ids = [s.id for s in rows if is_google_news_url(s.url)]
    if not eligible_ids:
        return 0
    await session.execute(
        update(Story).where(Story.id.in_(eligible_ids)).values(resolve_state="pending")
    )
    return len(eligible_ids)


async def run_url_resolution(
    *,
    session_factory: SessionFactory,
    event_ids: list[UUID],
    resolver: Resolver = resolve_google_news_url,
    per_run: int = RESOLVE_PER_RUN,
    delay_seconds: float = POLITENESS_DELAY_SECONDS,
) -> ResolveResult:
    async with _owned_session(session_factory) as s:
        await mark_eligible(s, event_ids)
        await s.commit()

    async with _owned_session(session_factory) as s:
        rows = (
            await s.execute(
                select(Story.id, Story.url)
                .where(Story.resolve_state == "pending")
                .order_by(Story.id)
                .limit(per_run + 1)
            )
        ).all()
    truncated = len(rows) > per_run
    rows = rows[:per_run]
    if truncated:
        log.info("url-resolution: per-run cap %d hit; more stories remain pending", per_run)

    resolved = failed = 0
    async with httpx.AsyncClient() as client:
        for story_id, url in rows:
            real: str | None = None
            try:
                real = await resolver(client, url)
            except Exception:
                log.exception("url-resolution decode crashed for story %s", story_id)
            async with _owned_session(session_factory) as s:
                story = await s.get(Story, story_id)
                if story is None:
                    continue
                story.resolve_attempts += 1
                if real:
                    story.resolved_url = real
                    story.resolve_state = "resolved"
                    resolved += 1
                elif story.resolve_attempts >= MAX_ATTEMPTS:
                    story.resolve_state = "failed"
                    failed += 1
                await s.commit()
            if delay_seconds:
                await asyncio.sleep(delay_seconds)

    async with _owned_session(session_factory) as s:
        still = (
            await s.execute(
                select(func.count()).select_from(Story).where(Story.resolve_state == "pending")
            )
        ).scalar_one()
    return ResolveResult(resolved=resolved, failed=failed, pending=still)
