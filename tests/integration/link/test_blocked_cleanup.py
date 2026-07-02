from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.link.blocked_cleanup import cleanup_blocked_sources
from upmovies.news.models import Event, EventStory, SourceDomain, Story


async def _film(session, slug):
    film = Film(tmdb_id=abs(hash(slug)) % 10_000_000, slug=slug, title="F")
    session.add(film)
    await session.flush()
    return film


async def _linked_story(session, film_id, url):
    s = Story(
        source="x",
        url=url,
        title="t",
        film_id=film_id,
        link_status="linked",
        fetched_at=datetime.now(UTC),
    )
    session.add(s)
    await session.flush()
    return s


async def _event(session, film_id, story_ids, *, confidence="confirmed"):
    now = datetime.now(UTC)
    ev = Event(
        film_id=film_id,
        event_type="casting",
        confidence=confidence,
        occurred_at=now,
        updated_at=now,
    )
    session.add(ev)
    await session.flush()
    for sid in story_ids:
        session.add(EventStory(event_id=ev.id, story_id=sid))
    await session.flush()
    return ev


async def _block(session, domain):
    now = datetime.now(UTC)
    session.add(
        SourceDomain(domain=domain, admin_override="block", first_seen_at=now, updated_at=now)
    )
    await session.flush()


async def test_rejects_blocked_story_and_deletes_only_source_event(session):
    film = await _film(session, "bc1")
    s = await _linked_story(session, film.id, "https://blocked.com/a")
    ev = await _event(session, film.id, [s.id])
    await _block(session, "blocked.com")

    report = await cleanup_blocked_sources(session, apply=True)

    assert report.blocked_domains == ["blocked.com"]
    assert report.stories_rejected == 1
    assert report.events_deleted == 1
    assert report.events_resummarized == 0

    story = (
        await session.execute(
            select(Story).where(Story.id == s.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert story.link_status == "rejected"
    assert story.film_id is None
    assert story.link_note == "source-blocked"
    assert (await session.get(Event, ev.id)) is None


async def test_keeps_mixed_event_and_detaches_blocked_story(session):
    film = await _film(session, "bc2")
    s_blocked = await _linked_story(session, film.id, "https://blocked.com/a")
    s_ok = await _linked_story(session, film.id, "https://variety.com/b")
    ev = await _event(session, film.id, [s_blocked.id, s_ok.id], confidence="confirmed")
    await _block(session, "blocked.com")

    report = await cleanup_blocked_sources(session, apply=True)

    assert report.stories_rejected == 1
    assert report.events_deleted == 0
    assert report.events_resummarized == 1

    # Event survives with its confidence untouched; the trusted story stays attached.
    kept = await session.get(Event, ev.id)
    assert kept is not None
    assert kept.confidence == "confirmed"
    remaining = (
        (await session.execute(select(EventStory.story_id).where(EventStory.event_id == ev.id)))
        .scalars()
        .all()
    )
    assert remaining == [s_ok.id]
    ok_story = (
        await session.execute(
            select(Story).where(Story.id == s_ok.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert ok_story.link_status == "linked"


async def test_dry_run_reports_but_changes_nothing(session):
    film = await _film(session, "bc3")
    s = await _linked_story(session, film.id, "https://blocked.com/a")
    ev = await _event(session, film.id, [s.id])
    await _block(session, "blocked.com")

    report = await cleanup_blocked_sources(session, apply=False)

    assert report.stories_rejected == 1
    assert report.events_deleted == 1

    story = (
        await session.execute(
            select(Story).where(Story.id == s.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert story.link_status == "linked"
    assert (await session.get(Event, ev.id)) is not None


async def test_low_tier_source_is_not_touched(session):
    film = await _film(session, "bc4")
    s = await _linked_story(session, film.id, "https://mshale.com/a")
    ev = await _event(session, film.id, [s.id])
    now = datetime.now(UTC)
    session.add(
        SourceDomain(
            domain="mshale.com",
            llm_tier="low",
            admin_override="none",
            first_seen_at=now,
            updated_at=now,
        )
    )
    await session.flush()

    report = await cleanup_blocked_sources(session, apply=True)

    assert report.stories_rejected == 0
    assert report.events_deleted == 0
    story = (
        await session.execute(
            select(Story).where(Story.id == s.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert story.link_status == "linked"
    assert (await session.get(Event, ev.id)) is not None
