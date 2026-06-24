import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm.client import BatchResult, Usage
from upmovies.news.models import Event, EventStory, Story


class FakeClient:
    """Serves both paths. `complete` (sequential Stage 1 + Stage 2) and `complete_batch`
    (batched Stage 1) route on the same `_decide` so outcomes are path-identical."""

    def __init__(self):
        self.complete_calls: list[dict] = []
        self.batch_requests: list | None = None
        self.cluster_batch_requests: list | None = None

    async def complete(self, *, model, system, messages, max_tokens=4096) -> str:
        self.complete_calls.append({"system": system, "messages": messages})
        return self._decide(system, messages)

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        self.complete_calls.append({"system": system, "messages": messages})
        return self._decide(system, messages), Usage()

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        reqs = list(requests)
        if reqs and "entity-linking classifier" in reqs[0].system[0]["text"]:
            self.batch_requests = reqs  # Stage-1 link batch
        else:
            self.cluster_batch_requests = reqs  # Stage-2 cluster batch
        return {
            r.custom_id: BatchResult(
                custom_id=r.custom_id,
                ok=True,
                text=self._decide(r.system, r.messages),
                usage=Usage(),
            )
            for r in reqs
        }

    def _decide(self, system, messages) -> str:
        if "entity-linking classifier" in system[0]["text"]:
            stories = json.loads(messages[0]["content"])
            return json.dumps(
                [{"id": s["id"], "film": 1, "confidence": 0.95, "reason": "about"} for s in stories]
            )
        new_ns = [s["n"] for s in json.loads(messages[0]["content"])["new_stories"]]
        return json.dumps(
            {
                "events": [
                    {
                        "existing": None,
                        "type": "trailer",
                        "confidence": "confirmed",
                        "stories": new_ns,
                    }
                ]
            }
        )


async def _story(url, *, published_offset_days, status="pending", title="Runner news"):
    now = datetime.now(UTC)
    return Story(
        source="X",
        url=url,
        title=title,
        published_at=now - timedelta(days=published_offset_days),
        link_status=status,
        raw={"summary": ""},
    )


async def _run(session, run_id, *, recency_days=45, use_batches=False, batch_size=10, client=None):
    return await run_link_ingest(
        session_factory=lambda: session,
        client=client or FakeClient(),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=recency_days,
        batch_size=batch_size,
        floor=0.7,
        use_batches=use_batches,
    )


@pytest.mark.parametrize("use_batches", [False, True])
async def test_links_then_clusters_recent_pending(session, use_batches):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all(
        [
            await _story("https://e/recent", published_offset_days=2),
            await _story("https://e/old", published_offset_days=400),
        ]
    )
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await _run(session, run_id, use_batches=use_batches)

    assert result.linked == 1
    rows = {
        s.url: s
        for s in (
            await session.execute(select(Story), execution_options={"populate_existing": True})
        )
        .scalars()
        .all()
    }
    assert rows["https://e/recent"].link_status == "linked"
    assert rows["https://e/recent"].film_id == film.id
    assert rows["https://e/old"].link_status == "pending"

    events = (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == "trailer"

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert (
        run.detail
        and "linked 1" in run.detail
        and "1 events" in run.detail
        and "stale-stage rejected" in run.detail
    )


@pytest.mark.parametrize("use_batches", [False, True])
async def test_rerun_is_noop_when_fully_processed(session, use_batches):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await _run(session, run_id, use_batches=use_batches)
    assert result.linked == 0 and result.rejected == 0


@pytest.mark.parametrize("use_batches", [False, True])
async def test_link_window_of_four_includes_story_past_the_feed_window(session, use_batches):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all(
        [
            # Published 3.5d ago: past the 3-day feed window but inside the 4-day link
            # window — the +1 margin must keep it eligible (fetched_at defaults to now).
            await _story("https://e/edge", published_offset_days=3.5),
            # Published 4.5d ago: outside the 4-day link window — stays pending.
            await _story("https://e/past", published_offset_days=4.5),
        ]
    )
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await _run(session, run_id, recency_days=4, use_batches=use_batches)

    assert result.linked == 1
    rows = {
        s.url: s
        for s in (
            await session.execute(select(Story), execution_options={"populate_existing": True})
        )
        .scalars()
        .all()
    }
    assert rows["https://e/edge"].link_status == "linked"
    assert rows["https://e/edge"].film_id == film.id
    assert rows["https://e/past"].link_status == "pending"


class _TaggedFailBatchClient(FakeClient):
    """Fails (or corrupts) the batched chunk whose payload contains a 'FAIL'-titled story."""

    def __init__(self, *, unparseable=False):
        super().__init__()
        self._unparseable = unparseable

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        reqs = list(requests)
        if not (reqs and "entity-linking classifier" in reqs[0].system[0]["text"]):
            return await super().complete_batch(reqs)  # Stage-2 cluster batch: serve normally
        self.batch_requests = reqs
        out = {}
        for r in reqs:
            stories = json.loads(r.messages[0]["content"])
            tainted = any(st["title"].startswith("FAIL") for st in stories)
            if tainted and not self._unparseable:
                out[r.custom_id] = BatchResult(
                    custom_id=r.custom_id, ok=False, error_type="errored", error_message="boom"
                )
            elif tainted:
                out[r.custom_id] = BatchResult(custom_id=r.custom_id, ok=True, text="not json")
            else:
                out[r.custom_id] = BatchResult(
                    custom_id=r.custom_id,
                    ok=True,
                    text=self._decide(r.system, r.messages),
                    usage=Usage(),
                )
        return out


@pytest.mark.parametrize("unparseable", [False, True])
async def test_batched_failed_chunk_stays_pending_others_commit(session, unparseable):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all(
        [
            await _story("https://e/good", published_offset_days=1, title="Runner news"),
            await _story("https://e/bad", published_offset_days=1, title="FAIL news"),
        ]
    )
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    # batch_size=1 → one chunk per story, so exactly the 'FAIL' chunk fails.
    result = await _run(
        session,
        run_id,
        use_batches=True,
        batch_size=1,
        client=_TaggedFailBatchClient(unparseable=unparseable),
    )

    assert result.linked == 1
    assert result.rejected == 0  # failed chunk leaves stories pending, not rejected
    rows = {
        s.url: s
        for s in (
            await session.execute(select(Story), execution_options={"populate_existing": True})
        )
        .scalars()
        .all()
    }
    assert rows["https://e/good"].link_status == "linked"
    assert rows["https://e/bad"].link_status == "pending"  # untouched → next run retries

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"


# ---------------------------------------------------------------------------
# Task 5: Whole-batch submit failure
# ---------------------------------------------------------------------------


class _RaisingBatchClient(FakeClient):
    """Simulates a batch poll that times out before reaching 'ended' status."""

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        raise TimeoutError("batch never reached 'ended'")


async def test_batched_whole_submit_failure_leaves_pending_and_run_succeeds(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all([await _story("https://e/x", published_offset_days=1)])
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await _run(session, run_id, use_batches=True, client=_RaisingBatchClient())

    assert result.linked == 0 and result.rejected == 0
    rows = {
        s.url: s
        for s in (
            await session.execute(select(Story), execution_options={"populate_existing": True})
        )
        .scalars()
        .all()
    }
    assert rows["https://e/x"].link_status == "pending"

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"  # Stage 2 still ran; one failure never fails the run


# ---------------------------------------------------------------------------
# Task 6: Request mapping (custom_id, cache_control, max_tokens)
# ---------------------------------------------------------------------------


async def test_batched_request_mapping(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all(
        [
            await _story("https://e/a", published_offset_days=1),
            await _story("https://e/b", published_offset_days=1),
        ]
    )
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    client = FakeClient()
    await _run(session, run_id, use_batches=True, batch_size=1, client=client)

    reqs = client.batch_requests
    assert reqs is not None
    assert {r.custom_id for r in reqs} == {"0", "1"}  # set of chunk indices
    for r in reqs:
        assert r.model == "claude-haiku-4-5"
        assert r.max_tokens == 2048  # == linker._MAX_TOKENS, for parity with the sequential path
        assert r.system[0]["cache_control"] == {"type": "ephemeral"}
        assert "entity-linking classifier" in r.system[0]["text"]


# ---------------------------------------------------------------------------
# Task 7: Flag routing (each path uses only its own client surface for Stage 1)
# ---------------------------------------------------------------------------


def _stage1_complete_calls(client) -> int:
    return sum(
        1 for c in client.complete_calls if "entity-linking classifier" in c["system"][0]["text"]
    )


async def test_flag_off_uses_sequential_only(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all([await _story("https://e/x", published_offset_days=1)])
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    client = FakeClient()
    await _run(session, run_id, use_batches=False, client=client)

    assert client.batch_requests is None  # complete_batch never called
    assert _stage1_complete_calls(client) == 1  # Stage 1 went through complete()


async def test_flag_on_uses_batch_for_stage1(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all([await _story("https://e/x", published_offset_days=1)])
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    client = FakeClient()
    await _run(session, run_id, use_batches=True, client=client)

    assert client.batch_requests is not None  # Stage 1 went through complete_batch()
    assert _stage1_complete_calls(client) == 0  # no Stage-1 complete() call
    assert client.cluster_batch_requests is not None  # Stage 2 also went through complete_batch()


# ---------------------------------------------------------------------------
# Task 7: Cluster batch request mapping
# ---------------------------------------------------------------------------


async def test_batched_cluster_request_mapping(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all([await _story("https://e/a", published_offset_days=1)])
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    client = FakeClient()
    await _run(session, run_id, use_batches=True, client=client)

    reqs = client.cluster_batch_requests
    assert reqs is not None
    assert {r.custom_id for r in reqs} == {
        str(film.id)
    }  # one cluster request per film, keyed by id
    for r in reqs:
        assert r.model == "claude-sonnet-4-6"
        assert r.max_tokens == 4096
        assert "cache_control" not in r.system[0]
        assert "distinct EVENTS" in r.system[0]["text"]


async def test_run_link_ingest_threads_cluster_max_tokens(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all([await _story("https://e/a", published_offset_days=1)])
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    client = FakeClient()
    await run_link_ingest(
        session_factory=lambda: session,
        client=client,
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=45,
        batch_size=1,
        floor=0.7,
        use_batches=True,
        cluster_max_tokens=7777,
    )

    reqs = client.cluster_batch_requests
    assert reqs is not None
    assert all(r.max_tokens == 7777 for r in reqs)


# ---------------------------------------------------------------------------
# Task 7: Batched cluster failure isolation
# ---------------------------------------------------------------------------


class _ClusterFailBatchClient:
    """Stage-level fake: serves cluster batches, failing the film whose title starts 'FAIL'."""

    def __init__(self):
        self.cluster_batch_requests: list | None = None

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        reqs = list(requests)
        self.cluster_batch_requests = reqs
        out = {}
        for r in reqs:
            payload = json.loads(r.messages[0]["content"])
            if payload["film"]["title"].startswith("FAIL"):
                out[r.custom_id] = BatchResult(
                    custom_id=r.custom_id, ok=False, error_type="errored", error_message="boom"
                )
            else:
                new_ns = [s["n"] for s in payload["new_stories"]]
                out[r.custom_id] = BatchResult(
                    custom_id=r.custom_id,
                    ok=True,
                    text=json.dumps(
                        {
                            "events": [
                                {
                                    "existing": None,
                                    "type": "trailer",
                                    "confidence": "confirmed",
                                    "stories": new_ns,
                                }
                            ]
                        }
                    ),
                    usage=Usage(),
                )
        return out


async def _linked_unclustered(session, film, url):
    s = Story(
        source="X",
        url=url,
        title="news",
        link_status="linked",
        film_id=film.id,
        published_at=datetime.now(UTC),
        raw={"summary": ""},
    )
    session.add(s)
    await session.flush()
    return s


async def test_batched_cluster_failure_is_isolated_per_film(session):
    from upmovies.link.pipeline import _cluster_stage_batched

    ok_film = Film(tmdb_id=1, title="Runner")
    fail_film = Film(tmdb_id=2, title="FAIL Movie")
    session.add_all([ok_film, fail_film])
    await session.flush()
    ok_story = await _linked_unclustered(session, ok_film, "https://e/ok")
    fail_story = await _linked_unclustered(session, fail_film, "https://e/fail")
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    events_created, stories_clustered, stories_rejected, _usage = await _cluster_stage_batched(
        session_factory=lambda: session,
        client=_ClusterFailBatchClient(),
        run_id=run_id,
        model="cluster-m",
        film_ids=[ok_film.id, fail_film.id],
        attach_limit=45,
        cluster_max_tokens=4096,
    )

    assert events_created == 1
    assert stories_clustered == 1
    assert stories_rejected == 0
    ok_events = (
        (await session.execute(select(Event).where(Event.film_id == ok_film.id))).scalars().all()
    )
    fail_events = (
        (await session.execute(select(Event).where(Event.film_id == fail_film.id))).scalars().all()
    )
    assert len(ok_events) == 1 and len(fail_events) == 0
    members = {el.story_id for el in (await session.execute(select(EventStory))).scalars().all()}
    assert ok_story.id in members and fail_story.id not in members
