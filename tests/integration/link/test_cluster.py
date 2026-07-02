import json
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from upmovies.catalog.models import Film
from upmovies.link.cluster import (
    ClusterParseError,
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

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        from upmovies.llm.client import Usage

        self.calls.append({"system": system, "messages": messages})
        return json.dumps(self._response), Usage()


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
    result, _usage = await cluster_film_events(
        session,
        client=client,
        model="m",
        film_id=film.id,
        attach_limit=45,
        run_date=date(2026, 1, 1),
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
    result, _usage = await cluster_film_events(
        session,
        client=client,
        model="m",
        film_id=film.id,
        attach_limit=45,
        run_date=date(2026, 1, 1),
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
    result, _usage = await cluster_film_events(
        session,
        client=client,
        model="m",
        film_id=film.id,
        attach_limit=45,
        run_date=date(2026, 1, 1),
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
    assert (
        await build_cluster_request(
            session, film_id=film.id, attach_limit=45, run_date=date(2026, 1, 1)
        )
        is None
    )


async def test_build_cluster_request_returns_none_for_missing_film(session):
    assert (
        await build_cluster_request(
            session, film_id=uuid.uuid4(), attach_limit=45, run_date=date(2026, 1, 1)
        )
        is None
    )


async def test_build_cluster_request_builds_payload_and_plan(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1", title="A")
    s2 = await _linked_story(session, film, "https://e/2", title="B")
    await session.commit()

    built = await build_cluster_request(
        session, film_id=film.id, attach_limit=45, run_date=date(2026, 1, 1)
    )
    assert built is not None
    system, messages, plan = built

    # NEU-377: cluster instructions are below Sonnet's 2048-tok cache floor and the
    # per-call payload is per-film (no shared prefix), so the block is intentionally
    # un-cached — a plain {"type": "text", "text": ...} block with no cache_control.
    assert "cache_control" not in system[0]
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

    built = await build_cluster_request(
        session, film_id=film.id, attach_limit=3650, run_date=date(2026, 1, 1)
    )
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
        session,
        custom_id=str(film.id),
        model="cluster-m",
        film_id=film.id,
        attach_limit=45,
        run_date=date(2026, 1, 1),
    )
    assert built is not None
    req, plan = built
    assert req.custom_id == str(film.id)
    assert req.model == "cluster-m"
    assert req.max_tokens == 4096
    assert "cache_control" not in req.system[0]
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
        attach_limit=45,
        max_tokens=9999,
        run_date=date(2026, 1, 1),
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
            session,
            custom_id=str(film.id),
            model="cluster-m",
            film_id=film.id,
            attach_limit=45,
            run_date=date(2026, 1, 1),
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


async def test_apply_raises_on_unparseable_response(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(film_id=film.id, existing_event_ids=[], unclustered_story_ids=[s1.id])
    with pytest.raises(ClusterParseError):
        await apply_cluster_decisions(session, plan=plan, raw="not json {")


# ---------------------------------------------------------------------------
# Task 2 — stale-stage rejection
# ---------------------------------------------------------------------------


async def test_build_cluster_request_captures_film_status(session):
    film = Film(tmdb_id=1, title="Runner", status="Post Production")
    session.add(film)
    await session.flush()
    await _linked_story(session, film, "https://e/1")
    await session.commit()

    built = await build_cluster_request(
        session, film_id=film.id, attach_limit=45, run_date=date(2026, 1, 1)
    )
    assert built is not None
    _system, _messages, plan = built
    assert plan.film_status == "Post Production"


async def test_apply_rejects_stale_stage_new_event(session):
    film = Film(tmdb_id=1, title="Starfighter", status="Post Production")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(
        film_id=film.id,
        existing_event_ids=[],
        unclustered_story_ids=[s1.id],
        film_status="Post Production",
    )
    raw = json.dumps(
        {
            "events": [
                {"existing": None, "type": "casting", "confidence": "confirmed", "stories": [1]}
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 0
    assert result.stories_clustered == 0
    assert result.stories_rejected == 1
    assert (
        await session.execute(select(Event).where(Event.film_id == film.id))
    ).scalars().all() == []
    assert (await session.execute(select(EventStory))).scalars().all() == []
    refreshed = (await session.execute(select(Story).where(Story.id == s1.id))).scalar_one()
    assert refreshed.link_status == "rejected"
    assert refreshed.film_id is None
    assert refreshed.link_confidence is None
    assert refreshed.link_note == "stale-stage:casting"


async def test_apply_rejects_stale_production_wrap_new_event(session):
    film = Film(tmdb_id=1, title="Brand New Day", status="Post Production")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/wrap")
    await session.commit()

    plan = ClusterPlan(
        film_id=film.id,
        existing_event_ids=[],
        unclustered_story_ids=[s1.id],
        film_status="Post Production",
    )
    raw = json.dumps(
        {
            "events": [
                {
                    "existing": None,
                    "type": "production_wrap",
                    "confidence": "confirmed",
                    "stories": [1],
                }
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 0
    assert result.stories_rejected == 1
    assert (
        await session.execute(select(Event).where(Event.film_id == film.id))
    ).scalars().all() == []
    assert (await session.execute(select(EventStory))).scalars().all() == []
    refreshed = (await session.execute(select(Story).where(Story.id == s1.id))).scalar_one()
    assert refreshed.link_status == "rejected"
    assert refreshed.film_id is None
    assert refreshed.link_confidence is None
    assert refreshed.link_note == "stale-stage:production_wrap"


async def test_apply_keeps_production_wrap_on_in_production_film(session):
    film = Film(tmdb_id=1, title="Runner", status="In Production")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/wrap")
    await session.commit()

    plan = ClusterPlan(
        film_id=film.id,
        existing_event_ids=[],
        unclustered_story_ids=[s1.id],
        film_status="In Production",
    )
    raw = json.dumps(
        {
            "events": [
                {
                    "existing": None,
                    "type": "production_wrap",
                    "confidence": "confirmed",
                    "stories": [1],
                }
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 1 and result.stories_rejected == 0
    events = (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    assert len(events) == 1 and events[0].event_type == "production_wrap"


async def test_apply_keeps_late_stage_event_on_wrapped_film(session):
    film = Film(tmdb_id=1, title="Starfighter", status="Post Production")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(
        film_id=film.id,
        existing_event_ids=[],
        unclustered_story_ids=[s1.id],
        film_status="Post Production",
    )
    raw = json.dumps(
        {
            "events": [
                {"existing": None, "type": "trailer", "confidence": "confirmed", "stories": [1]}
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 1 and result.stories_rejected == 0
    events = (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    assert len(events) == 1 and events[0].event_type == "trailer"


async def test_apply_keeps_early_stage_event_on_unwrapped_film(session):
    film = Film(tmdb_id=1, title="Runner", status="In Production")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(
        film_id=film.id,
        existing_event_ids=[],
        unclustered_story_ids=[s1.id],
        film_status="In Production",
    )
    raw = json.dumps(
        {
            "events": [
                {"existing": None, "type": "casting", "confidence": "confirmed", "stories": [1]}
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 1 and result.stories_rejected == 0


async def test_apply_does_not_gate_attach_to_existing(session):
    film = Film(tmdb_id=1, title="Starfighter", status="Post Production")
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
        film_id=film.id,
        existing_event_ids=[event.id],
        unclustered_story_ids=[second.id],
        film_status="Post Production",
    )
    raw = json.dumps({"events": [{"existing": 1, "stories": [1]}]})
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.stories_rejected == 0 and result.stories_clustered == 1
    member_ids = {el.story_id for el in (await session.execute(select(EventStory))).scalars().all()}
    assert member_ids == {first.id, second.id}


# ---------------------------------------------------------------------------
# NEU-372 — attach lookback decoupled from the story-recency window
# ---------------------------------------------------------------------------


async def test_build_cluster_request_includes_event_older_than_recency_window(session):
    """An event last touched well beyond LINK_RECENCY_DAYS (4) is still offered as an
    attach candidate — the lookback no longer depends on updated_at recency."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    old = datetime(2026, 1, 1, tzinfo=UTC)
    event = Event(
        film_id=film.id,
        event_type="casting",
        confidence="confirmed",
        occurred_at=old,
        updated_at=old,  # far older than the 4-day window
    )
    session.add(event)
    await session.flush()
    member = await _linked_story(session, film, "https://e/old", title="Old casting beat")
    session.add(EventStory(event_id=event.id, story_id=member.id))
    await _linked_story(session, film, "https://e/new", title="Re-report")  # unclustered
    await session.commit()

    built = await build_cluster_request(
        session, film_id=film.id, attach_limit=25, run_date=date(2026, 1, 1)
    )
    assert built is not None
    _system, messages, plan = built
    payload = json.loads(messages[0]["content"])
    assert [e["type"] for e in payload["existing_events"]] == ["casting"]
    assert plan.existing_event_ids == [event.id]


async def test_build_cluster_request_caps_existing_events_to_attach_limit(session):
    """With more events than attach_limit, only the most-recent N by occurred_at are
    offered, presented oldest->newest so 1-based positional indices stay stable."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    events = []
    for day in (1, 2, 3):
        ev = Event(
            film_id=film.id,
            event_type="casting",
            confidence="confirmed",
            occurred_at=datetime(2026, 1, day, tzinfo=UTC),
        )
        session.add(ev)
        await session.flush()
        st = await _linked_story(session, film, f"https://e/m{day}", title=f"beat day {day}")
        session.add(EventStory(event_id=ev.id, story_id=st.id))
        events.append(ev)
    await _linked_story(session, film, "https://e/new")  # unclustered -> request is built
    await session.commit()

    built = await build_cluster_request(
        session, film_id=film.id, attach_limit=2, run_date=date(2026, 1, 1)
    )
    assert built is not None
    _system, messages, plan = built
    payload = json.loads(messages[0]["content"])
    titles = [h for e in payload["existing_events"] for h in e["headlines"]]
    assert titles == ["beat day 2", "beat day 3"]  # 2 most recent, oldest->newest
    assert plan.existing_event_ids == [events[1].id, events[2].id]


async def test_build_cluster_request_caps_headlines_per_event(session):
    """An event with many member stories contributes at most 3 headlines, most-recent first."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    event = Event(
        film_id=film.id,
        event_type="casting",
        confidence="confirmed",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(event)
    await session.flush()
    for i in range(1, 6):
        st = Story(
            source="X",
            url=f"https://e/m{i}",
            title=f"headline {i}",
            link_status="linked",
            film_id=film.id,
            published_at=datetime(2026, 1, i, tzinfo=UTC),
            raw={"summary": ""},
        )
        session.add(st)
        await session.flush()
        session.add(EventStory(event_id=event.id, story_id=st.id))
    await _linked_story(session, film, "https://e/new")  # unclustered -> request is built
    await session.commit()

    built = await build_cluster_request(
        session, film_id=film.id, attach_limit=25, run_date=date(2026, 1, 1)
    )
    assert built is not None
    _system, messages, _plan = built
    payload = json.loads(messages[0]["content"])
    assert len(payload["existing_events"]) == 1
    assert payload["existing_events"][0]["headlines"] == ["headline 5", "headline 4", "headline 3"]


async def test_cluster_film_events_attaches_across_day_window_without_moving_occurred_at(session):
    """NEU-372 end-to-end: a re-report of a beat logged long ago attaches to the existing
    event (no duplicate) and leaves occurred_at anchored to the original beat date."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    old = datetime(2026, 1, 1, tzinfo=UTC)
    event = Event(
        film_id=film.id,
        event_type="casting",
        confidence="confirmed",
        occurred_at=old,
        updated_at=old,
    )
    session.add(event)
    await session.flush()
    first = await _linked_story(session, film, "https://e/1", title="Original casting")
    session.add(EventStory(event_id=event.id, story_id=first.id))
    rereport = await _linked_story(session, film, "https://e/2", title="Casting, revisited")
    await session.commit()

    client = FakeClient({"events": [{"existing": 1, "stories": [1]}]})
    result, _usage = await cluster_film_events(
        session,
        client=client,
        model="m",
        film_id=film.id,
        attach_limit=25,
        run_date=date(2026, 1, 1),
    )
    await session.commit()

    assert result.events_created == 0
    assert result.stories_clustered == 1
    events = (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    assert len(events) == 1  # attached, no duplicate
    member_ids = {el.story_id for el in (await session.execute(select(EventStory))).scalars().all()}
    assert member_ids == {first.id, rereport.id}
    refreshed = (await session.execute(select(Event).where(Event.id == event.id))).scalar_one()
    assert refreshed.occurred_at == old  # unchanged — timeline stays anchored
    assert refreshed.updated_at > old  # attach bumped updated_at


# ---------------------------------------------------------------------------
# Task 3 — region persistence on release_date events
# ---------------------------------------------------------------------------


async def test_release_date_event_persists_region(session):
    film = Film(tmdb_id=2, title="Runner")
    session.add(film)
    await session.flush()
    await _linked_story(session, film, "https://e/region-1")
    await session.commit()

    client = FakeClient(
        {
            "events": [
                {
                    "existing": None,
                    "type": "release_date",
                    "confidence": "confirmed",
                    "region": "IN",
                    "stories": [1],
                }
            ]
        }
    )
    await cluster_film_events(
        session,
        client=client,
        model="m",
        film_id=film.id,
        attach_limit=45,
        run_date=date(2026, 1, 1),
    )
    await session.commit()

    event = (await session.execute(select(Event).where(Event.film_id == film.id))).scalar_one()
    assert event.event_type == "release_date"
    assert event.region == "IN"


# ---------------------------------------------------------------------------
# NEU-453 — off_topic cluster backstop
# ---------------------------------------------------------------------------


async def test_apply_drops_off_topic_new_event(session):
    """A story whose real subject is a different film is assigned type 'off_topic' and
    dropped (rejected, no event), mirroring the stale-stage reject path."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    s1 = await _linked_story(session, film, "https://e/1")
    await session.commit()

    plan = ClusterPlan(film_id=film.id, existing_event_ids=[], unclustered_story_ids=[s1.id])
    raw = json.dumps(
        {"events": [{"existing": None, "type": "off_topic", "confidence": None, "stories": [1]}]}
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 0
    assert result.stories_clustered == 0
    assert result.stories_rejected == 1
    assert (
        await session.execute(select(Event).where(Event.film_id == film.id))
    ).scalars().all() == []
    assert (await session.execute(select(EventStory))).scalars().all() == []
    refreshed = (await session.execute(select(Story).where(Story.id == s1.id))).scalar_one()
    assert refreshed.link_status == "rejected"
    assert refreshed.film_id is None
    assert refreshed.link_confidence is None
    assert refreshed.link_note == "off-topic"


# ---------------------------------------------------------------------------
# NEU-449 — release-date staleness backstop
# ---------------------------------------------------------------------------


async def test_apply_rejects_casting_for_already_released_film(session):
    import datetime as _dt

    film = Film(
        tmdb_id=4242,
        title="Angry Birds Movie 3",
        status="In Production",
        release_date=_dt.date(2026, 1, 1),
    )
    session.add(film)
    await session.flush()
    s1 = await _linked_story(
        session, film, "https://example.com/psalm", title="Psalm's acting debut"
    )
    await session.commit()

    plan = ClusterPlan(
        film_id=film.id,
        existing_event_ids=[],
        unclustered_story_ids=[s1.id],
        film_status="In Production",
        film_release_date=_dt.date(2026, 1, 1),
        run_date=_dt.date(2026, 7, 1),
    )
    raw = json.dumps(
        {
            "events": [
                {"existing": None, "type": "casting", "confidence": "confirmed", "stories": [1]}
            ]
        }
    )
    result = await apply_cluster_decisions(session, plan=plan, raw=raw)
    await session.commit()

    assert result.events_created == 0
    assert result.stories_rejected == 1
    no_events = (
        (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    )
    assert no_events == []
    refreshed = (await session.execute(select(Story).where(Story.id == s1.id))).scalar_one()
    assert refreshed.link_status == "rejected"
    assert refreshed.link_note == "stale-stage:casting"


async def test_non_release_date_event_ignores_region(session):
    film = Film(tmdb_id=3, title="Runner")
    session.add(film)
    await session.flush()
    await _linked_story(session, film, "https://e/region-2")
    await session.commit()

    # A model that wrongly returns a region on a casting event must not persist it.
    client = FakeClient(
        {
            "events": [
                {
                    "existing": None,
                    "type": "casting",
                    "confidence": "confirmed",
                    "region": "IN",
                    "stories": [1],
                }
            ]
        }
    )
    await cluster_film_events(
        session,
        client=client,
        model="m",
        film_id=film.id,
        attach_limit=45,
        run_date=date(2026, 1, 1),
    )
    await session.commit()

    event = (await session.execute(select(Event).where(Event.film_id == film.id))).scalar_one()
    assert event.event_type == "casting"
    assert event.region is None
