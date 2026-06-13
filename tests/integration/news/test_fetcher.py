import httpx
import respx
from sqlalchemy import select

from tests.fixtures.news.sample_feeds import (
    ATOM_FEED,
    MALFORMED_FEED,
    RSS_FEED,
    RSS_FEED_WITH_DUPLICATE_URLS,
)
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.news.feeds import FeedSource
from upmovies.news.fetcher import run_feeds_ingest
from upmovies.news.models import Story

RSS_URL = "https://deadline.com/feed"
ATOM_URL = "https://variety.com/feed"
DUPE_URL = "https://collider.com/feed"
BAD_URL = "https://broken.example/feed"


def _xml(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"Content-Type": "application/xml"})


async def _stories(session) -> list[Story]:
    result = await session.execute(
        select(Story).order_by(Story.url), execution_options={"populate_existing": True}
    )
    return list(result.scalars().all())


async def _run(session, sources):
    run_id = await create_run(session, kind="feeds")
    await session.commit()
    result = await run_feeds_ingest(session_factory=lambda: session, run_id=run_id, sources=sources)
    return run_id, result


@respx.mock
async def test_inserts_new_stories_from_multiple_feeds(session):
    respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))
    respx.get(ATOM_URL).mock(return_value=_xml(ATOM_FEED))

    _, result = await _run(
        session,
        [FeedSource("Deadline", RSS_URL), FeedSource("Variety", ATOM_URL)],
    )

    assert result.feeds_processed == 2
    assert result.feeds_failed == 0
    assert result.stories_inserted == 3
    stories = await _stories(session)
    assert {s.url for s in stories} == {
        "https://deadline.com/2026/06/story-1",
        "https://deadline.com/2026/06/story-2",
        "https://variety.com/2026/film/trailer-1",
    }
    assert {s.source for s in stories} == {"Deadline", "Variety"}


@respx.mock
async def test_duplicate_urls_are_deduped_within_a_feed(session):
    respx.get(DUPE_URL).mock(return_value=_xml(RSS_FEED_WITH_DUPLICATE_URLS))

    _, result = await _run(session, [FeedSource("Collider", DUPE_URL)])

    assert result.stories_inserted == 1
    assert len(await _stories(session)) == 1


@respx.mock
async def test_rerun_does_not_reinsert_existing_urls(session):
    respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))
    sources = [FeedSource("Deadline", RSS_URL)]

    await _run(session, sources)
    _, second = await _run(session, sources)

    assert second.stories_inserted == 0
    assert len(await _stories(session)) == 2


@respx.mock
async def test_one_failing_feed_does_not_fail_the_run(session):
    respx.get(BAD_URL).mock(return_value=httpx.Response(500))
    respx.get(ATOM_URL).mock(return_value=_xml(ATOM_FEED))

    run_id, result = await _run(
        session,
        [FeedSource("Broken", BAD_URL), FeedSource("Variety", ATOM_URL)],
    )

    assert result.feeds_failed == 1
    assert result.feeds_processed == 1
    assert {s.url for s in await _stories(session)} == {"https://variety.com/2026/film/trailer-1"}

    row = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "succeeded"


@respx.mock
async def test_malformed_feed_handled_gracefully(session):
    respx.get(BAD_URL).mock(return_value=_xml(MALFORMED_FEED))
    respx.get(ATOM_URL).mock(return_value=_xml(ATOM_FEED))

    _, result = await _run(
        session,
        [FeedSource("Garbage", BAD_URL), FeedSource("Variety", ATOM_URL)],
    )

    # Malformed content yields no entries but must not crash or abort the run.
    assert result.stories_inserted == 1
    assert len(await _stories(session)) == 1


@respx.mock
async def test_run_finalized_succeeded_with_progress_counts(session):
    respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))

    run_id, _ = await _run(session, [FeedSource("Deadline", RSS_URL)])

    row = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.items_processed == 2
