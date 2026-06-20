import json
from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.link.cluster import cluster_film_events
from upmovies.news.models import Event, EventStory, Story


class FakeClient:
    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    async def complete(self, *, model, system, messages, max_tokens=4096) -> str:
        self.calls.append({"system": system, "messages": messages})
        return json.dumps(self._response)


async def _linked_story(session, film, url, *, title="Runner news"):
    story = Story(
        source="X",
        url=url,
        title=title,
        link_status="linked",
        film_id=film.id,
        published_at=datetime.now(UTC),
        raw={"summary": ""},
    )
    session.add(story)
    await session.flush()
    return story


async def test_creates_new_event_for_unclustered_stories(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    story = await _linked_story(session, film, "https://e/1")
    await session.commit()

    client = FakeClient(
        {
            "events": [
                {
                    "existing": None,
                    "type": "trailer",
                    "confidence": "confirmed",
                    "stories": [str(story.id)],
                }
            ]
        }
    )
    result = await cluster_film_events(
        session, client=client, model="m", film_id=film.id, recency_days=45
    )
    await session.commit()

    assert result.events_created == 1
    assert result.stories_clustered == 1
    events = (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == "trailer"
    assert events[0].confidence == "confirmed"
    links = (await session.execute(select(EventStory))).scalars().all()
    assert [el.story_id for el in links] == [story.id]


async def test_attaches_to_existing_event(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    first = await _linked_story(session, film, "https://e/1")
    event = Event(
        film_id=film.id, event_type="casting", confidence="confirmed", occurred_at=datetime.now(UTC)
    )
    session.add(event)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=first.id))
    second = await _linked_story(session, film, "https://e/2")  # unclustered
    await session.commit()

    client = FakeClient({"events": [{"existing": 1, "stories": [str(second.id)]}]})
    result = await cluster_film_events(
        session, client=client, model="m", film_id=film.id, recency_days=45
    )
    await session.commit()

    assert result.events_created == 0
    assert result.stories_clustered == 1
    events = (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    assert len(events) == 1  # no new event
    member_ids = {el.story_id for el in (await session.execute(select(EventStory))).scalars().all()}
    assert member_ids == {first.id, second.id}


async def test_noop_when_nothing_unclustered(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    story = await _linked_story(session, film, "https://e/1")
    event = Event(
        film_id=film.id, event_type="casting", confidence="confirmed", occurred_at=datetime.now(UTC)
    )
    session.add(event)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.commit()

    client = FakeClient({"events": []})
    result = await cluster_film_events(
        session, client=client, model="m", film_id=film.id, recency_days=45
    )
    assert result == result.__class__(0, 0)
    assert client.calls == []  # short-circuits before calling the model
