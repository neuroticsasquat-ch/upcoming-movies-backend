"""Feed fetcher: pull each configured feed, parse RSS/Atom, normalize entries, and land
them in `news.story` deduped by `url`. Per-feed error isolation — one bad feed never
fails the whole run. Wires into the ingest_run tracker (kind `feeds`), mirroring the
TMDB ingestion service so the orchestration layer can drive both pipelines uniformly.

ToS-clean: feeds only, no article-body scraping. `film_id` stays null — entity linking
is a later project."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import struct_time
from typing import Any
from uuid import UUID

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.ingest.runs import finalize_run, record_progress
from upmovies.news.feeds import (
    FeedSource,
    feed_sources,
    is_google_source,
    per_film_google_sources,
)
from upmovies.news.models import Story
from upmovies.news.outlet import outlet_from_entry

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

# Entry keys worth retaining verbatim in `story.raw` for later entity-linking work.
_RAW_KEYS = ("title", "link", "summary", "published", "updated", "id", "author", "source")

# Identify ourselves to feed hosts. Some (e.g. Empire's onebauer/CloudFront host)
# reject the bare `python-httpx/...` default UA with a 403, and politely-behaved
# feed readers are expected to send a contactable User-Agent anyway.
_USER_AGENT = (
    "UpcomingMoviesBot/1.0 (+https://github.com/neuroticsasquat-ch/upcoming-movies-backend)"
)


def _looks_blocked(resp: httpx.Response) -> bool:
    """True when Google is throttling/captcha-ing us rather than serving the RSS feed."""
    if resp.status_code in (403, 429):
        return True
    if resp.status_code == 200 and "xml" not in resp.headers.get("content-type", "").lower():
        return True
    return False


@dataclass
class StoryEntry:
    source: str
    url: str
    title: str
    published_at: datetime | None
    outlet: str | None
    raw: dict[str, Any]


@dataclass
class FeedsIngestResult:
    feeds_processed: int
    feeds_failed: int
    stories_inserted: int
    films_queried: int = 0
    blocked: bool = False


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
    resolve_outlet = is_google_source(source)
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
                outlet=outlet_from_entry(entry) if resolve_outlet else None,
                raw={k: entry[k] for k in _RAW_KEYS if k in entry},
            )
        )
    return entries


def drop_stale(entries: Sequence[StoryEntry], *, cutoff: datetime) -> list[StoryEntry]:
    return [e for e in entries if e.published_at is None or e.published_at >= cutoff]


async def _film_titles(session_factory: SessionFactory) -> list[str]:
    """Lightweight roster read for per-film queries — the deliberate news→catalog
    coupling. Title column only (not link.roster.build_roster's heavier rows)."""
    async with _owned_session(session_factory) as s:
        rows = await s.execute(select(Film.title).order_by(Film.title))
        return list(rows.scalars().all())


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
            "outlet": e.outlet,
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
    recency_days: int,
    per_film_enabled: bool = False,
    per_film_throttle: float = 1.0,
    sources: Sequence[FeedSource] | None = None,
    timeout: float = 30.0,
) -> FeedsIngestResult:
    if sources is None:
        sources = feed_sources(recency_days)
    cutoff = datetime.now(UTC) - timedelta(days=recency_days)
    feeds_processed = 0
    feeds_failed = 0
    stories_inserted = 0
    films_queried = 0
    blocked = False
    block_reason: str = ""

    async def _ingest_entries(source: FeedSource, content: bytes) -> int:
        entries = drop_stale(parse_feed(source.name, content), cutoff=cutoff)
        async with _owned_session(session_factory) as s:
            inserted = await upsert_stories(s, entries)
            await record_progress(s, run_id, processed_delta=inserted)
            await s.commit()
        return inserted

    async def _record_failure() -> None:
        async with _owned_session(session_factory) as s:
            await record_progress(s, run_id, failed_delta=1)
            await s.commit()

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as client:
        # Phase A — trade + broad feeds (per-feed isolation; one bad feed never aborts).
        for source in sources:
            try:
                resp = await client.get(source.url)
                resp.raise_for_status()
                stories_inserted += await _ingest_entries(source, resp.content)
                feeds_processed += 1
            except Exception:
                log.exception("feed ingest failed for %s (%s)", source.name, source.url)
                feeds_failed += 1
                await _record_failure()

        # Phase B — per-film Google queries (serialized; abort the phase on a block).
        if per_film_enabled:
            titles = await _film_titles(session_factory)
            for source in per_film_google_sources(titles, recency_days):
                try:
                    resp = await client.get(source.url)
                    if _looks_blocked(resp):
                        blocked = True
                        block_reason = f"HTTP {resp.status_code}"
                        log.error(
                            "per-film fetch blocked after %d films (%s)",
                            films_queried,
                            block_reason,
                        )
                        break
                    resp.raise_for_status()
                    stories_inserted += await _ingest_entries(source, resp.content)
                    films_queried += 1
                except Exception:
                    log.exception("per-film feed failed (%s)", source.url)
                    feeds_failed += 1
                    await _record_failure()
                await asyncio.sleep(per_film_throttle)

    run_status = "failed" if blocked else "succeeded"
    if blocked:
        detail = (
            f"{feeds_processed} feeds ok, {feeds_failed} failed, "
            f"blocked after {films_queried} films, {stories_inserted} stories inserted"
        )
    else:
        detail = (
            f"{feeds_processed} feeds ok, {feeds_failed} failed, "
            f"{films_queried} films queried, {stories_inserted} stories inserted"
        )
    async with _owned_session(session_factory) as s:
        await finalize_run(
            s,
            run_id,
            status=run_status,
            detail=detail,
            error=(f"per-film fetch blocked ({block_reason})" if blocked else None),
        )
        await s.commit()

    return FeedsIngestResult(
        feeds_processed, feeds_failed, stories_inserted, films_queried, blocked
    )
