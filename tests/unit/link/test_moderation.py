import pytest
from sqlalchemy import func, select

from upmovies.link import moderation
from upmovies.news.models import Event, EventStory, EventSummary, Story


async def test_delink_only_source_removes_event(session, make_film, add_event):
    film = await make_film(slug="dl-1")
    event = await add_event(
        film=film,
        summary="Bogus.",
        sources=({"url": "https://x.test/a", "source": "ScreenRant", "title": "t"},),
    )

    result = await moderation.delink_story(session, event_id=event.id, url="https://x.test/a")
    await session.commit()

    assert result.delinked == 1 and result.event_removed is True
    assert (await session.get(Event, event.id)) is None  # event + summary cascaded
    story = (
        await session.execute(select(Story).where(Story.url == "https://x.test/a"))
    ).scalar_one()
    assert story.link_status == "rejected" and story.film_id is None
    assert story.link_note == "manual-unlink"


async def test_delink_one_of_two_keeps_event_and_clears_summary(session, make_film, add_event):
    film = await make_film(slug="dl-2")
    event = await add_event(
        film=film,
        summary="Two sources.",
        sources=(
            {"url": "https://x.test/a", "source": "A", "title": "ta"},
            {"url": "https://x.test/b", "source": "B", "title": "tb"},
        ),
    )

    result = await moderation.delink_story(session, event_id=event.id, url="https://x.test/a")
    await session.commit()

    assert result.event_removed is False and result.resummarize_queued is True
    kept = await session.get(Event, event.id, execution_options={"populate_existing": True})
    assert kept is not None
    remaining = (
        await session.execute(
            select(func.count()).select_from(EventStory).where(EventStory.event_id == event.id)
        )
    ).scalar_one()
    assert remaining == 1
    summary = (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one_or_none()
    assert summary is None  # summary deleted → synthesize regenerates fresh next run


async def test_delete_event_rejects_all_and_cascades(session, make_film, add_event):
    film = await make_film(slug="dl-3")
    event = await add_event(
        film=film,
        summary="Whole event bogus.",
        sources=(
            {"url": "https://x.test/a", "source": "A", "title": "ta"},
            {"url": "https://x.test/b", "source": "B", "title": "tb"},
        ),
    )

    result = await moderation.delete_event(session, event_id=event.id)
    await session.commit()

    assert result.delinked == 2 and result.event_removed is True
    assert (await session.get(Event, event.id)) is None
    assert (
        await session.execute(
            select(func.count()).select_from(EventSummary).where(EventSummary.event_id == event.id)
        )
    ).scalar_one() == 0
    statuses = (await session.execute(select(Story.link_status))).scalars().all()
    assert set(statuses) == {"rejected"}


async def test_delink_unknown_event_raises(session):
    from uuid import uuid4

    with pytest.raises(moderation.EventNotFound):
        await moderation.delink_story(session, event_id=uuid4(), url="https://x.test/a")


async def test_delink_url_not_in_event_raises(session, make_film, add_event):
    film = await make_film(slug="dl-4")
    event = await add_event(
        film=film,
        summary="s",
        sources=({"url": "https://x.test/a", "source": "A", "title": "ta"},),
    )
    with pytest.raises(moderation.StoryNotInEvent):
        await moderation.delink_story(session, event_id=event.id, url="https://x.test/nope")
