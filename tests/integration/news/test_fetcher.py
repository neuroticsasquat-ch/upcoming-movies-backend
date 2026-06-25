from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import respx
from sqlalchemy import select

from tests.fixtures.news.sample_feeds import (
    ATOM_FEED,
    MALFORMED_FEED,
    RSS_FEED,
    RSS_FEED_WITH_DUPLICATE_URLS,
)
from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.news.feeds import FeedSource, per_film_google_sources
from upmovies.news.fetcher import run_feeds_ingest
from upmovies.news.models import Story

RSS_URL = "https://deadline.com/feed"
ATOM_URL = "https://variety.com/feed"
DUPE_URL = "https://collider.com/feed"
BAD_URL = "https://broken.example/feed"
GATE_URL = "https://gate.example/feed"


def _xml(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"Content-Type": "application/xml"})


def _rss(items: list[tuple[str, str, datetime | None]]) -> str:
    rows = []
    for title, url, dt in items:
        date_line = f"      <pubDate>{format_datetime(dt)}</pubDate>\n" if dt else ""
        rows.append(
            "    <item>\n"
            f"      <title>{title}</title>\n"
            f"      <link>{url}</link>\n"
            f"{date_line}"
            "    </item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel><title>Gate</title>\n'
        + "\n".join(rows)
        + "\n</channel></rss>\n"
    )


async def _stories(session) -> list[Story]:
    result = await session.execute(
        select(Story).order_by(Story.url), execution_options={"populate_existing": True}
    )
    return list(result.scalars().all())


async def _run(session, sources, recency_days=36500):
    run_id = await create_run(session, kind="feeds")
    await session.commit()
    result = await run_feeds_ingest(
        session_factory=lambda: session,
        run_id=run_id,
        recency_days=recency_days,
        sources=sources,
    )
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
async def test_requests_send_identifying_user_agent(session):
    route = respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))

    await _run(session, [FeedSource("Deadline", RSS_URL)])

    # Some feed hosts (e.g. Empire via onebauer/CloudFront) 403 the bare
    # `python-httpx/...` default UA, so the fetcher must identify itself.
    ua = route.calls.last.request.headers.get("user-agent", "")
    assert ua and not ua.startswith("python-httpx")


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


@respx.mock
async def test_stale_dated_entries_dropped_keeping_recent_and_undated(session):
    now = datetime.now(UTC)
    respx.get(GATE_URL).mock(
        return_value=_xml(
            _rss(
                [
                    ("Recent", "https://gate.example/recent", now - timedelta(days=2)),
                    ("Stale", "https://gate.example/stale", now - timedelta(days=100)),
                    ("Undated", "https://gate.example/undated", None),
                ]
            )
        )
    )

    _, result = await _run(session, [FeedSource("Gate", GATE_URL)], recency_days=14)

    assert result.stories_inserted == 2
    assert {s.url for s in await _stories(session)} == {
        "https://gate.example/recent",
        "https://gate.example/undated",
    }


async def _seed_films(session, titles):
    for i, t in enumerate(titles, start=1):
        session.add(Film(tmdb_id=i, title=t))
    await session.commit()


async def _run_pf(session, base_sources, *, recency_days=36500, throttle=0.0):
    run_id = await create_run(session, kind="feeds")
    await session.commit()
    result = await run_feeds_ingest(
        session_factory=lambda: session,
        run_id=run_id,
        recency_days=recency_days,
        per_film_enabled=True,
        per_film_throttle=throttle,
        sources=base_sources,
    )
    return run_id, result


@respx.mock
async def test_flag_off_does_not_query_roster_or_fetch_per_film(session):
    await _seed_films(session, ["Spider-Man"])
    respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))
    # per_film defaults off at the function level — no per-film route is registered,
    # so if Phase B fired it would raise (respx blocks unmocked requests).
    _, result = await _run(session, [FeedSource("Deadline", RSS_URL)])
    assert result.films_queried == 0
    assert result.blocked is False


@respx.mock
async def test_flag_on_fetches_per_film_and_dedupes_by_url(session):
    await _seed_films(session, ["Spider-Man"])
    pf = per_film_google_sources(["Spider-Man"], 36500)[0]
    respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))
    respx.get(pf.url).mock(
        return_value=_xml(_rss([("Spidey news", "https://news.example/spidey", None)]))
    )

    run_id, result = await _run_pf(session, [FeedSource("Deadline", RSS_URL)])

    assert result.films_queried == 1
    assert result.blocked is False
    urls = {s.url for s in await _stories(session)}
    assert "https://news.example/spidey" in urls
    row = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "succeeded"


@respx.mock
async def test_block_signal_aborts_phase_b_and_fails_run(session):
    # Queried title-sorted: "Shrek 5" sorts before "Spider-Man", so the first lands a
    # story and the second triggers the block/abort.
    await _seed_films(session, ["Shrek 5", "Spider-Man"])
    first = per_film_google_sources(["Shrek 5"], 36500)[0]
    blocked_src = per_film_google_sources(["Spider-Man"], 36500)[0]
    respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))
    respx.get(first.url).mock(
        return_value=_xml(_rss([("Shrek news", "https://news.example/shrek", None)]))
    )
    respx.get(blocked_src.url).mock(return_value=httpx.Response(429))

    run_id, result = await _run_pf(session, [FeedSource("Deadline", RSS_URL)])

    assert result.blocked is True
    assert result.films_queried == 1  # Shrek 5 succeeded before Spider-Man triggered the block
    # Phase-A + the pre-block per-film story survive.
    assert "https://news.example/shrek" in {s.url for s in await _stories(session)}
    row = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error and "blocked" in row.error.lower()


@respx.mock
async def test_ordinary_per_film_error_isolates_and_run_succeeds(session):
    await _seed_films(session, ["Spider-Man"])
    pf = per_film_google_sources(["Spider-Man"], 36500)[0]
    respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))
    respx.get(pf.url).mock(return_value=httpx.Response(500))  # transient, not a block signal

    run_id, result = await _run_pf(session, [FeedSource("Deadline", RSS_URL)])

    assert result.blocked is False
    assert result.feeds_failed == 1
    row = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "succeeded"


@respx.mock
async def test_throttle_is_applied_between_per_film_requests(session, monkeypatch):
    await _seed_films(session, ["Spider-Man", "Shrek 5"])
    for t in ["Spider-Man", "Shrek 5"]:
        respx.get(per_film_google_sources([t], 36500)[0].url).mock(
            return_value=_xml(_rss([("x", f"https://news.example/{t}", None)]))
        )
    respx.get(RSS_URL).mock(return_value=_xml(RSS_FEED))

    sleeps: list[float] = []

    async def fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr("upmovies.news.fetcher.asyncio.sleep", fake_sleep)
    await _run_pf(session, [FeedSource("Deadline", RSS_URL)], throttle=1.0)
    assert len(sleeps) == 2 and all(s == 1.0 for s in sleeps)  # one sleep per per-film request


@respx.mock
async def test_per_film_story_persists_outlet_and_raw_source(session):
    await _seed_films(session, ["Spider-Man"])
    pf = per_film_google_sources(["Spider-Man"], 36500)[0]
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Google News</title>'
        "<item><title>Spidey Lands Director - Deadline</title>"
        "<link>https://news.example/spidey</link>"
        '<source url="https://deadline.com">Deadline</source></item>'
        "</channel></rss>"
    )
    respx.get(pf.url).mock(return_value=_xml(feed))

    await _run_pf(session, [])  # no Phase-A feeds, per-film only

    stories = await _stories(session)
    assert len(stories) == 1
    assert stories[0].outlet == "Deadline"
    # `<source>` is retained in raw JSONB (round-trips through Postgres cleanly).
    raw = stories[0].raw
    assert raw is not None and raw["source"]["title"] == "Deadline"


@respx.mock
async def test_per_film_title_filter_drops_off_topic_and_leaves_trades(session):
    await _seed_films(session, ["Spider-Man"])
    pf = per_film_google_sources(["Spider-Man"], 36500)[0]
    respx.get(RSS_URL).mock(
        return_value=_xml(
            _rss([("Totally unrelated trade headline", "https://deadline.com/x", None)])
        )
    )
    respx.get(pf.url).mock(
        return_value=_xml(
            _rss(
                [
                    ("Spider-Man swings into a new trailer", "https://news.example/spidey", None),
                    ("Local bakery wins an award", "https://news.example/bakery", None),
                ]
            )
        )
    )
    run_id = await create_run(session, kind="feeds")
    await session.commit()
    result = await run_feeds_ingest(
        session_factory=lambda: session,
        run_id=run_id,
        recency_days=36500,
        per_film_enabled=True,
        per_film_throttle=0.0,
        per_film_title_filter_enabled=True,
        per_film_title_match_min_ratio=0.5,
        sources=[FeedSource("Deadline", RSS_URL)],
    )

    urls = {s.url for s in await _stories(session)}
    assert "https://news.example/spidey" in urls  # on-topic per-film kept
    assert "https://news.example/bakery" not in urls  # off-topic per-film dropped
    assert "https://deadline.com/x" in urls  # trade feed NOT filtered
    assert result.stories_filtered == 1
