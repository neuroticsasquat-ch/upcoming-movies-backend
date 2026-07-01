from datetime import UTC, datetime

from sqlalchemy import func, select

from upmovies.catalog.models import Film
from upmovies.news.models import Event, EventStory, Story
from upmovies.synthesize.url_resolution import mark_displayed_eligible, run_url_resolution

_FILM_SEQ = 0


async def _google_story(session, *, url, event=None):
    """Create a Story at `url` attached to an event (creating a Film+Event if none given).
    Returns (event, story)."""
    global _FILM_SEQ
    if event is None:
        _FILM_SEQ += 1
        film = Film(tmdb_id=1000 + _FILM_SEQ, title=f"Film {_FILM_SEQ}")
        session.add(film)
        await session.flush()
        event = Event(
            film_id=film.id,
            event_type="casting",
            confidence="confirmed",
            occurred_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()
    story = Story(
        source="Google News: per-film", url=url, title="Headline", published_at=datetime.now(UTC)
    )
    session.add(story)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.flush()
    return event, story


async def _unattached_story(session, *, url):
    """A Story with NO EventStory row (not displayed)."""
    story = Story(
        source="Google News: per-film", url=url, title="Headline", published_at=datetime.now(UTC)
    )
    session.add(story)
    await session.flush()
    return story


async def test_mark_displayed_eligible_flips_all_displayed_google_news(session):
    _, s1 = await _google_story(session, url="https://news.google.com/rss/articles/CBMiaaa")
    _, s2 = await _google_story(session, url="https://news.google.com/rss/articles/CBMibbb")
    n = await mark_displayed_eligible(session)
    await session.commit()
    r1 = await session.get(Story, s1.id, execution_options={"populate_existing": True})
    r2 = await session.get(Story, s2.id, execution_options={"populate_existing": True})
    assert n == 2
    assert r1.resolve_state == "pending"
    assert r2.resolve_state == "pending"


async def test_mark_displayed_eligible_skips_non_displayed_and_non_google(session):
    _, trade_story = await _google_story(session, url="https://variety.com/2026/film/x")
    undisplayed = await _unattached_story(
        session, url="https://news.google.com/rss/articles/CBMiundisplayed"
    )
    await session.commit()
    n = await mark_displayed_eligible(session)
    await session.commit()
    trade = await session.get(Story, trade_story.id, execution_options={"populate_existing": True})
    und = await session.get(Story, undisplayed.id, execution_options={"populate_existing": True})
    assert n == 0
    assert trade.resolve_state == "none"  # displayed but not a Google News URL
    assert und.resolve_state == "none"  # Google News URL but not displayed


async def test_mark_displayed_eligible_is_idempotent(session):
    await _google_story(session, url="https://news.google.com/rss/articles/CBMiidem")
    assert await mark_displayed_eligible(session) == 1
    await session.commit()
    assert await mark_displayed_eligible(session) == 0  # already pending → nothing to mark
    await session.commit()


async def test_run_resolution_marks_then_resolves(session):
    _, gn_story = await _google_story(
        session, url="https://news.google.com/rss/articles/CBMisuccess"
    )
    await session.commit()

    async def fake_resolver(client, url):
        return "https://variety.com/real-article"

    result = await run_url_resolution(
        session_factory=lambda: session,
        resolver=fake_resolver,
        delay_seconds=0.0,
    )

    refreshed = await session.get(Story, gn_story.id, execution_options={"populate_existing": True})
    assert result.marked == 1
    assert result.resolved == 1
    assert result.failed == 0
    assert result.pending == 0
    assert refreshed.resolve_state == "resolved"
    assert refreshed.resolved_url == "https://variety.com/real-article"
    assert refreshed.resolve_attempts == 1


async def test_run_resolution_caps_retries_then_fails(session):
    _, gn_story = await _google_story(session, url="https://news.google.com/rss/articles/CBMifail")
    await session.commit()

    async def always_none(client, url):
        return None

    for _ in range(3):
        await run_url_resolution(
            session_factory=lambda: session,
            resolver=always_none,
            delay_seconds=0.0,
        )
    refreshed = await session.get(Story, gn_story.id, execution_options={"populate_existing": True})
    assert refreshed.resolve_attempts == 3
    assert refreshed.resolve_state == "failed"
    assert refreshed.resolved_url is None


async def test_run_resolution_per_run_cap_leaves_remainder_pending(session):
    event, _ = await _google_story(session, url="https://news.google.com/rss/articles/CBMicap0")
    await _google_story(session, url="https://news.google.com/rss/articles/CBMicap1", event=event)
    await _google_story(session, url="https://news.google.com/rss/articles/CBMicap2", event=event)
    await session.commit()

    async def always_none(client, url):
        return None

    result = await run_url_resolution(
        session_factory=lambda: session,
        resolver=always_none,
        per_run=2,
        delay_seconds=0.0,
    )
    assert result.marked == 3  # all 3 displayed Google News stories marked eligible
    assert result.pending == 3  # none resolved; all remain pending
    attempted = (
        await session.execute(
            select(func.count()).select_from(Story).where(Story.resolve_attempts == 1)
        )
    ).scalar_one()
    assert attempted == 2  # exactly 2 attempted this run (per_run cap)


async def test_run_resolution_honors_custom_max_attempts(session):
    _, gn_story = await _google_story(session, url="https://news.google.com/rss/articles/CBMimax1")
    await session.commit()

    async def always_none(client, url):
        return None

    result = await run_url_resolution(
        session_factory=lambda: session,
        resolver=always_none,
        max_attempts=1,
        delay_seconds=0.0,
    )
    refreshed = await session.get(Story, gn_story.id, execution_options={"populate_existing": True})
    assert result.failed == 1
    assert refreshed.resolve_state == "failed"  # cap of 1 → failed after a single miss
    assert refreshed.resolve_attempts == 1
