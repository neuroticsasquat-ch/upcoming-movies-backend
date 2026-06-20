from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from upmovies.catalog.models import Film
from upmovies.news.models import Event, EventStory, Story


async def _film_and_story(session, url="https://e/1"):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    story = Story(source="X", url=url, title="t")
    session.add(story)
    await session.flush()
    return film, story


async def test_event_rejects_bad_type(session):
    film, _ = await _film_and_story(session)
    session.add(
        Event(
            film_id=film.id,
            event_type="bogus",
            confidence="confirmed",
            occurred_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_event_rejects_bad_confidence(session):
    film, _ = await _film_and_story(session)
    session.add(
        Event(
            film_id=film.id, event_type="casting", confidence="maybe", occurred_at=datetime.now(UTC)
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_one_event_per_story_enforced(session):
    film, story = await _film_and_story(session)
    e1 = Event(
        film_id=film.id, event_type="casting", confidence="confirmed", occurred_at=datetime.now(UTC)
    )
    e2 = Event(
        film_id=film.id, event_type="trailer", confidence="confirmed", occurred_at=datetime.now(UTC)
    )
    session.add_all([e1, e2])
    await session.flush()
    session.add(EventStory(event_id=e1.id, story_id=story.id))
    await session.flush()
    session.add(EventStory(event_id=e2.id, story_id=story.id))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_valid_event_and_link_persist(session):
    film, story = await _film_and_story(session)
    event = Event(
        film_id=film.id,
        event_type="release_date",
        confidence="rumored",
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.commit()  # must not raise
    assert event.created_at is not None
