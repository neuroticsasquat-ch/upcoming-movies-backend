"""SOURCE-JUDGE sub-stage (NEU-454): runs after LINK and before CLUSTER over the linked,
still-unclustered stories. Resolves Google-News URLs so we know the real publisher domain,
judges any unknown domains (cached judge-once), and hard-drops stories whose effective tier
is admin-blocked so they never form or join an event. Owns its own sessions and commits per
step so one failure never rolls back the rest — mirrors synthesize/url_resolution.py."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.link.linker import Completer
from upmovies.llm.client import Usage
from upmovies.news.models import EventStory, Story
from upmovies.news.resolve import is_google_news_url, resolve_google_news_url
from upmovies.news.source_quality import (
    domain_for_story,
    effective_tier,
    get_source_domains,
    judge_domains,
    upsert_judgements,
)

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]
Resolver = Callable[[httpx.AsyncClient, str], Awaitable[str | None]]


@dataclass
class SourceQualityResult:
    resolved: int
    judged: int
    blocked: int


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


async def _linked_unclustered(session: AsyncSession) -> list[Story]:
    clustered = exists().where(EventStory.story_id == Story.id)
    return list(
        (
            await session.execute(
                select(Story).where(
                    Story.link_status == "linked",
                    Story.film_id.is_not(None),
                    ~clustered,
                )
            )
        )
        .scalars()
        .all()
    )


async def run_source_quality_stage(
    *,
    session_factory: SessionFactory,
    client: Completer,
    judge_model: str,
    resolver: Resolver = resolve_google_news_url,
    unresolved_tier: str = "acceptable",
) -> tuple[SourceQualityResult, Usage]:
    # 1. Resolve Google-News URLs for the linked batch so we know the publisher domain.
    async with _owned_session(session_factory) as s:
        stories = await _linked_unclustered(s)
        pending = [
            (st.id, st.url)
            for st in stories
            if st.resolved_url is None and is_google_news_url(st.url)
        ]

    resolved = 0
    if pending:
        async with httpx.AsyncClient() as http:
            for story_id, url in pending:
                real: str | None = None
                try:
                    real = await resolver(http, url)
                except Exception:
                    log.exception("source-stage: resolve crashed for story %s", story_id)
                async with _owned_session(session_factory) as s:
                    story = await s.get(Story, story_id)
                    if story is None:
                        continue
                    story.resolve_attempts += 1
                    if real:
                        story.resolved_url = real
                        story.resolve_state = "resolved"
                        resolved += 1
                    else:
                        # Leave resolve_state unchanged (remains 'none') — synthesize's
                        # capped-retry contract (up to max_attempts) governs transient failures.
                        # Terminal 'failed' here would block future retries.
                        pass
                    await s.commit()

    # 2. Re-read the batch (now with resolved_urls), compute domains, judge unknowns.
    async with _owned_session(session_factory) as s:
        stories = await _linked_unclustered(s)
        domain_by_sid = {
            st.id: domain_for_story(url=st.url, resolved_url=st.resolved_url) for st in stories
        }
        sample_by_domain: dict[str, str] = {}
        for st in stories:
            d = domain_by_sid[st.id]
            if d and d not in sample_by_domain:
                sample_by_domain[d] = st.title
        all_domains = set(sample_by_domain)
        known = await get_source_domains(s, all_domains)
        unknown = sorted(all_domains - set(known))

    usage = Usage()
    judged = 0
    if unknown:
        items = [{"domain": d, "sample_headline": sample_by_domain[d]} for d in unknown]
        verdicts, usage = await judge_domains(client=client, model=judge_model, items=items)
        async with _owned_session(session_factory) as s:
            judged = await upsert_judgements(s, verdicts, model=judge_model, now=datetime.now(UTC))
            await s.commit()

    # 3. Hard-drop stories whose effective tier is admin-blocked.
    blocked = 0
    async with _owned_session(session_factory) as s:
        stories = await _linked_unclustered(s)
        domain_by_sid = {
            st.id: domain_for_story(url=st.url, resolved_url=st.resolved_url) for st in stories
        }
        rows = await get_source_domains(s, [d for d in domain_by_sid.values() if d])
        now = datetime.now(UTC)
        for st in stories:
            domain = domain_by_sid.get(st.id)
            row = rows.get(domain) if domain else None
            tier = effective_tier(
                llm_tier=row.llm_tier if row else None,
                admin_override=row.admin_override if row else "none",
                unresolved_default=unresolved_tier,
            )
            if tier == "blocked":
                st.link_status = "rejected"
                st.film_id = None
                st.link_confidence = None
                st.link_note = "source-blocked"
                st.linked_at = now
                blocked += 1
        await s.commit()

    return SourceQualityResult(resolved=resolved, judged=judged, blocked=blocked), usage
