from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.link.google_cleanup import cleanup_google_sources
from upmovies.news.models import Event, EventStory, EventSummary, Story

GOOGLE = "Google News: per-film"
GOOGLE_BROAD = "Google News: casting"


async def _film(session, slug):
    film = Film(tmdb_id=abs(hash(slug)) % 10_000_000, slug=slug, title="F")
    session.add(film)
    await session.flush()
    return film


async def _story(session, *, source, url, film_id=None, link_status="linked"):
    s = Story(
        source=source,
        url=url,
        title="t",
        film_id=film_id,
        link_status=link_status,
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


async def test_rejects_linked_google_story_and_deletes_only_source_event(session):
    film = await _film(session, "gc1")
    s = await _story(session, source=GOOGLE, url="https://g.example/a", film_id=film.id)
    ev = await _event(session, film.id, [s.id])

    report = await cleanup_google_sources(session, apply=True)

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
    assert story.link_note == "google-paused"
    assert (await session.get(Event, ev.id)) is None


async def test_rejects_pending_google_story_so_it_never_clusters(session):
    # A pending Google story is not in any event yet; it must be rejected so the next
    # link run cannot cluster it (this is the class that would otherwise re-fill the app).
    s = await _story(
        session, source=GOOGLE_BROAD, url="https://g.example/pending", link_status="pending"
    )

    report = await cleanup_google_sources(session, apply=True)

    assert report.stories_rejected == 1
    assert report.events_deleted == 0
    story = (
        await session.execute(
            select(Story).where(Story.id == s.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert story.link_status == "rejected"
    assert story.link_note == "google-paused"


async def test_keeps_mixed_event_and_detaches_google_story(session):
    film = await _film(session, "gc2")
    s_google = await _story(session, source=GOOGLE, url="https://g.example/a", film_id=film.id)
    s_trade = await _story(
        session, source="Deadline", url="https://deadline.com/b", film_id=film.id
    )
    ev = await _event(session, film.id, [s_google.id, s_trade.id])
    session.add(
        EventSummary(
            event_id=ev.id,
            summary="A neutral summary.",
            model="claude-haiku-4-5",
            prompt_version="1",
            source_updated_at=datetime.now(UTC),
        )
    )
    await session.flush()

    report = await cleanup_google_sources(session, apply=True)

    assert report.stories_rejected == 1
    assert report.events_deleted == 0
    assert report.events_resummarized == 1

    kept = await session.get(Event, ev.id)
    assert kept is not None
    assert kept.confidence == "confirmed"
    remaining = (
        (await session.execute(select(EventStory.story_id).where(EventStory.event_id == ev.id)))
        .scalars()
        .all()
    )
    assert remaining == [s_trade.id]
    trade = (
        await session.execute(
            select(Story).where(Story.id == s_trade.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert trade.link_status == "linked"
    summary = (
        await session.execute(select(EventSummary).where(EventSummary.event_id == ev.id))
    ).scalar_one_or_none()
    assert summary is None


async def test_trade_stories_are_left_untouched(session):
    film = await _film(session, "gc3")
    s = await _story(session, source="Variety", url="https://variety.com/a", film_id=film.id)
    ev = await _event(session, film.id, [s.id])

    report = await cleanup_google_sources(session, apply=True)

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


async def test_dry_run_reports_but_changes_nothing(session):
    film = await _film(session, "gc4")
    s = await _story(session, source=GOOGLE, url="https://g.example/a", film_id=film.id)
    ev = await _event(session, film.id, [s.id])

    report = await cleanup_google_sources(session, apply=False)

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
