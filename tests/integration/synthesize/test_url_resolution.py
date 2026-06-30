from datetime import UTC, datetime

from sqlalchemy import func, select

from upmovies.catalog.models import Film
from upmovies.news.models import Event, EventStory, Story
from upmovies.synthesize.url_resolution import mark_eligible, run_url_resolution

_FILM_SEQ = 0


async def _google_story(session, *, url, event=None):
    """Create a Story at `url`. If `event` is None, also create a Film+Event and
    attach; otherwise attach to the given event. Returns (event, story)."""
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


async def test_mark_eligible_flips_google_news_stories(session):
    event, gn_story = await _google_story(
        session, url="https://news.google.com/rss/articles/CBMiabc"
    )
    n = await mark_eligible(session, [event.id])
    await session.commit()
    refreshed = await session.get(Story, gn_story.id, execution_options={"populate_existing": True})
    assert n == 1
    assert refreshed.resolve_state == "pending"


async def test_mark_eligible_skips_non_google_and_out_of_scope_events(session):
    event_a, trade_story = await _google_story(session, url="https://variety.com/2026/film/x")
    event_b, gn_story = await _google_story(
        session, url="https://news.google.com/rss/articles/CBMidef"
    )
    n = await mark_eligible(session, [event_a.id])  # only event_a in scope
    await session.commit()
    trade = await session.get(Story, trade_story.id, execution_options={"populate_existing": True})
    gn = await session.get(Story, gn_story.id, execution_options={"populate_existing": True})
    assert n == 0  # trade story is not a Google News URL
    assert trade.resolve_state == "none"
    assert gn.resolve_state == "none"  # event_b out of scope → untouched (leave-as-is)


async def test_run_resolution_success_sets_resolved_url(session):
    event, gn_story = await _google_story(
        session, url="https://news.google.com/rss/articles/CBMisuccess"
    )
    await session.commit()

    async def fake_resolver(client, url):
        return "https://variety.com/real-article"

    result = await run_url_resolution(
        session_factory=lambda: session,
        event_ids=[event.id],
        resolver=fake_resolver,
        delay_seconds=0.0,
    )

    refreshed = await session.get(Story, gn_story.id, execution_options={"populate_existing": True})
    assert result.resolved == 1
    assert result.failed == 0
    assert result.pending == 0
    assert refreshed.resolve_state == "resolved"
    assert refreshed.resolved_url == "https://variety.com/real-article"
    assert refreshed.resolve_attempts == 1


async def test_run_resolution_caps_retries_then_fails(session):
    event, gn_story = await _google_story(
        session, url="https://news.google.com/rss/articles/CBMifail"
    )
    await session.commit()

    async def always_none(client, url):
        return None

    for _ in range(3):
        await run_url_resolution(
            session_factory=lambda: session,
            event_ids=[event.id],
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
        event_ids=[event.id],
        resolver=always_none,
        per_run=2,
        delay_seconds=0.0,
    )
    assert (
        result.pending == 3
    )  # all 3 still pending; 2 were attempted this run (per_run cap), 1 not selected
    attempted = (
        await session.execute(
            select(func.count()).select_from(Story).where(Story.resolve_attempts == 1)
        )
    ).scalar_one()
    assert attempted == 2  # exactly 2 attempted this run (per_run cap)
