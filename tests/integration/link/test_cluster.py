import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from upmovies.catalog.models import Film
from upmovies.link.cluster import (
    ClusterPlan,
    apply_cluster_decisions,
    build_cluster_batch_request,
    build_cluster_request,
    cluster_film_events,
)
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
                    "stories": [1],
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

    client = FakeClient({"events": [{"existing": 1, "stories": [1]}]})
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


# ---------------------------------------------------------------------------
# Task 1 — build_cluster_request
# ---------------------------------------------------------------------------


async def test_build_cluster_request_returns_none_when_no_unclustered(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await session.commit()
    assert await build_cluster_request(session, film_id=film.id, recency_days=45) is None


async def test_build_cluster_request_returns_none_for_missing_film(session):
    assert await build_cluster_request(session, film_id=uuid.uuid4(), recency_days=45) is None


async def test_build_cluster_request_builds_payload_and_plan(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1", title="A")
    s2 = await _linked_story(session, film, "https://e/2", title="B")
    await session.commit()

    built = await build_cluster_request(session, film_id=film.id, recency_days=45)
    assert built is not None
    system, messages, plan = built

    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "distinct EVENTS" in system[0]["text"]
    payload = json.loads(messages[0]["content"])
    assert payload["film"]["title"] == "Runner"
    assert payload["existing_events"] == []
    assert {s["n"] for s in payload["new_stories"]} == {1, 2}
    assert {s["title"] for s in payload["new_stories"]} == {"A", "B"}

    assert isinstance(plan, ClusterPlan)
    assert plan.film_id == film.id
    assert plan.existing_event_ids == []
    assert set(plan.unclustered_story_ids) == {s1.id, s2.id}


# ---------------------------------------------------------------------------
# Task 2 — apply_cluster_decisions
# ---------------------------------------------------------------------------


async def test_apply_creates_new_event(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(film_id=film.id, existing_event_ids=[], unclustered_story_ids=[s1.id])
    raw = json.dumps(
        {
            "events": [
                {
                    "existing": None,
                    "type": "trailer",
                    "confidence": "confirmed",
                    "stories": [1],
                }
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 1
    assert result.stories_clustered == 1
    events = (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    assert len(events) == 1 and events[0].event_type == "trailer"
    links = (await session.execute(select(EventStory))).scalars().all()
    assert [el.story_id for el in links] == [s1.id]


async def test_apply_attaches_to_existing_by_plan_index(session):
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
    second = await _linked_story(session, film, "https://e/2")
    await session.commit()

    plan = ClusterPlan(
        film_id=film.id, existing_event_ids=[event.id], unclustered_story_ids=[second.id]
    )
    raw = json.dumps({"events": [{"existing": 1, "stories": [1]}]})
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 0
    assert result.stories_clustered == 1
    member_ids = {el.story_id for el in (await session.execute(select(EventStory))).scalars().all()}
    assert member_ids == {first.id, second.id}


async def test_apply_skips_invalid_new_event(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(film_id=film.id, existing_event_ids=[], unclustered_story_ids=[s1.id])
    raw = json.dumps(
        {
            "events": [
                {
                    "existing": None,
                    "type": "not-a-type",
                    "confidence": "confirmed",
                    "stories": [1],
                }
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 0 and result.stories_clustered == 0
    assert (await session.execute(select(EventStory))).scalars().all() == []


async def test_apply_uses_build_time_event_order_across_sessions(session, test_engine):
    """The crux of the design: apply maps the LLM's positional `existing` index off the
    plan's captured order, NOT a re-derived recency query."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    e1 = Event(
        film_id=film.id,
        event_type="casting",
        confidence="confirmed",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    e2 = Event(
        film_id=film.id,
        event_type="trailer",
        confidence="confirmed",
        occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    session.add_all([e1, e2])
    await session.flush()
    c1 = await _linked_story(session, film, "https://e/c1")
    c2 = await _linked_story(session, film, "https://e/c2")
    session.add_all(
        [EventStory(event_id=e1.id, story_id=c1.id), EventStory(event_id=e2.id, story_id=c2.id)]
    )
    s1 = await _linked_story(session, film, "https://e/s1")
    s2 = await _linked_story(session, film, "https://e/s2")
    await session.commit()

    built = await build_cluster_request(session, film_id=film.id, recency_days=3650)
    assert built is not None
    _system, _messages, plan = built
    assert plan.existing_event_ids == [e1.id, e2.id]  # ordered by occurred_at at build time

    # Flip occurred_at so a re-derived query would now order [e2, e1]; the plan must not change.
    e1.occurred_at = datetime(2026, 3, 1, tzinfo=UTC)
    await session.commit()

    n_s1 = plan.unclustered_story_ids.index(s1.id) + 1
    n_s2 = plan.unclustered_story_ids.index(s2.id) + 1
    raw = json.dumps(
        {
            "events": [
                {"existing": 1, "stories": [n_s1]},
                {
                    "existing": None,
                    "type": "casting",
                    "confidence": "rumored",
                    "stories": [n_s2],
                },
            ]
        }
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as b:
        result = await apply_cluster_decisions(b, plan=plan, raw=raw)
        await b.commit()

    assert result.events_created == 1
    assert result.stories_clustered == 2
    async with maker() as c:
        e1_members = (
            (await c.execute(select(EventStory.story_id).where(EventStory.event_id == e1.id)))
            .scalars()
            .all()
        )
        e2_members = (
            (await c.execute(select(EventStory.story_id).where(EventStory.event_id == e2.id)))
            .scalars()
            .all()
        )
    assert s1.id in e1_members  # plan index 1 → e1 despite the occurred_at flip
    assert s1.id not in e2_members


# ---------------------------------------------------------------------------
# Task 4 — build_cluster_batch_request
# ---------------------------------------------------------------------------


async def test_build_cluster_batch_request_wraps_into_batch_request(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    built = await build_cluster_batch_request(
        session, custom_id=str(film.id), model="cluster-m", film_id=film.id, recency_days=45
    )
    assert built is not None
    req, plan = built
    assert req.custom_id == str(film.id)
    assert req.model == "cluster-m"
    assert req.max_tokens == 4096
    assert req.system[0]["cache_control"] == {"type": "ephemeral"}
    assert "distinct EVENTS" in req.system[0]["text"]
    assert plan.film_id == film.id
    assert set(plan.unclustered_story_ids) == {s1.id}


async def test_build_cluster_batch_request_honours_max_tokens(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await _linked_story(session, film, "https://e/1")
    await session.commit()

    built = await build_cluster_batch_request(
        session,
        custom_id=str(film.id),
        model="cluster-m",
        film_id=film.id,
        recency_days=45,
        max_tokens=9999,
    )
    assert built is not None
    req, _plan = built
    assert req.max_tokens == 9999


async def test_build_cluster_batch_request_none_when_nothing_to_cluster(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await session.commit()
    assert (
        await build_cluster_batch_request(
            session, custom_id=str(film.id), model="cluster-m", film_id=film.id, recency_days=45
        )
        is None
    )


# ---------------------------------------------------------------------------
# Task 3 — intra-group dedup
# ---------------------------------------------------------------------------


async def test_apply_dedups_repeated_story_within_group(session):
    """Failure mode 2: a story number repeated inside one group must insert once,
    not violate event_story_pkey."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(film_id=film.id, existing_event_ids=[], unclustered_story_ids=[s1.id])
    raw = json.dumps(
        {
            "events": [
                {
                    "existing": None,
                    "type": "trailer",
                    "confidence": "confirmed",
                    "stories": [1, 1],
                }
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.stories_clustered == 1
    links = (await session.execute(select(EventStory))).scalars().all()
    assert [el.story_id for el in links] == [s1.id]


async def test_apply_returns_empty_on_unparseable_response(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(film_id=film.id, existing_event_ids=[], unclustered_story_ids=[s1.id])
    result = await apply_cluster_decisions(session, plan=plan, raw="not json {")
    await session.commit()

    assert result.events_created == 0 and result.stories_clustered == 0
    assert (await session.execute(select(EventStory))).scalars().all() == []
