"""Feed fetcher: pull each configured feed, parse RSS/Atom, normalize entries, and land
them in `news.story` deduped by `url`. Per-feed error isolation — one bad feed never
fails the whole run. Wires into the ingest_run tracker (kind `feeds`), mirroring the
TMDB ingestion service so the orchestration layer can drive both pipelines uniformly.

ToS-clean: feeds only, no article-body scraping. `film_id` stays null — entity linking
is a later project."""

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time
from typing import Any
from uuid import UUID

import feedparser
import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.runs import finalize_run, record_progress
from upmovies.news.feeds import FEED_SOURCES, FeedSource
from upmovies.news.models import Story

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

# Entry keys worth retaining verbatim in `story.raw` for later entity-linking work.
_RAW_KEYS = ("title", "link", "summary", "published", "updated", "id", "author")

# Identify ourselves to feed hosts. Some (e.g. Empire's onebauer/CloudFront host)
# reject the bare `python-httpx/...` default UA with a 403, and politely-behaved
# feed readers are expected to send a contactable User-Agent anyway.
_USER_AGENT = (
    "UpcomingMoviesBot/1.0 (+https://github.com/neuroticsasquat-ch/upcoming-movies-backend)"
)


@dataclass
class StoryEntry:
    source: str
    url: str
    title: str
    published_at: datetime | None
    raw: dict[str, Any]


@dataclass
class FeedsIngestResult:
    feeds_processed: int
    feeds_failed: int
    stories_inserted: int


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


def _published_at(entry: Any) -> datetime | None:
    parsed: struct_time | None = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime(parsed[0], parsed[1], parsed[2], parsed[3], parsed[4], parsed[5], tzinfo=UTC)


def parse_feed(source: str, content: str | bytes) -> list[StoryEntry]:
    """Parse one feed's body into normalized entries. Tolerant of junk — feedparser
    returns no entries for unparseable content rather than raising. Entries missing a
    url or title are dropped (both are NOT NULL, and url is the dedupe key)."""
    parsed = feedparser.parse(content)
    entries: list[StoryEntry] = []
    for entry in parsed.entries:
        url = entry.get("link")
        title = entry.get("title")
        if not isinstance(url, str) or not isinstance(title, str) or not url or not title:
            continue
        entries.append(
            StoryEntry(
                source=source,
                url=url,
                title=title,
                published_at=_published_at(entry),
                raw={k: entry[k] for k in _RAW_KEYS if k in entry},
            )
        )
    return entries


async def upsert_stories(session: AsyncSession, entries: Sequence[StoryEntry]) -> int:
    """Insert new stories, skipping any whose url already exists. Returns the count of
    rows actually inserted. Caller owns the transaction."""
    # Collapse intra-feed url duplicates so a single INSERT can't violate the unique
    # index against itself; existing rows are handled by ON CONFLICT DO NOTHING.
    by_url: dict[str, StoryEntry] = {}
    for entry in entries:
        by_url.setdefault(entry.url, entry)
    if not by_url:
        return 0
    values = [
        {
            "source": e.source,
            "url": e.url,
            "title": e.title,
            "published_at": e.published_at,
            "raw": e.raw,
        }
        for e in by_url.values()
    ]
    stmt = (
        insert(Story)
        .values(values)
        .on_conflict_do_nothing(index_elements=[Story.url])
        .returning(Story.id)
    )
    result = await session.execute(stmt)
    return len(result.scalars().all())


async def run_feeds_ingest(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    sources: Sequence[FeedSource] = FEED_SOURCES,
    timeout: float = 30.0,
) -> FeedsIngestResult:
    feeds_processed = 0
    feeds_failed = 0
    stories_inserted = 0

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as client:
        for source in sources:
            try:
                resp = await client.get(source.url)
                resp.raise_for_status()
                entries = parse_feed(source.name, resp.content)
                async with _owned_session(session_factory) as s:
                    inserted = await upsert_stories(s, entries)
                    await record_progress(s, run_id, processed_delta=inserted)
                    await s.commit()
                feeds_processed += 1
                stories_inserted += inserted
            except Exception:
                log.exception("feed ingest failed for %s (%s)", source.name, source.url)
                feeds_failed += 1
                async with _owned_session(session_factory) as s:
                    await record_progress(s, run_id, failed_delta=1)
                    await s.commit()

    async with _owned_session(session_factory) as s:
        await finalize_run(
            s,
            run_id,
            status="succeeded",
            detail=(
                f"{feeds_processed} feeds ok, {feeds_failed} failed, "
                f"{stories_inserted} stories inserted"
            ),
        )
        await s.commit()

    return FeedsIngestResult(feeds_processed, feeds_failed, stories_inserted)
