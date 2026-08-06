import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import _cluster_stage_sequential, run_link_ingest
from upmovies.llm.client import CallResult
from upmovies.news.models import Event, EventStory, Story


class FakeClient:
    """Serves Stage 1 (link) and Stage 2 (cluster) off the same `_decide`, recording every
    call's system blocks, messages, and max_tokens."""

    def __init__(self):
        self.complete_calls: list[dict] = []

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        self.complete_calls.append(
            {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens}
        )
        return calls.record(CallResult(text=self._decide(system, messages)))

    def _decide(self, system, messages) -> str:
        if "entity-linking classifier" in system[0]["text"]:
            payload = json.loads(messages[0]["content"])
            stories = payload["stories"]
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


def _link_calls(client) -> list[dict]:
    return [
        c for c in client.complete_calls if "entity-linking classifier" in c["system"][0]["text"]
    ]


def _cluster_calls(client) -> list[dict]:
    return [
        c
        for c in client.complete_calls
        if "entity-linking classifier" not in c["system"][0]["text"]
    ]


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


async def _run(session, run_id, *, recency_days=45, batch_size=10, client=None):
    return await run_link_ingest(
        session_factory=lambda: session,
        client=client or FakeClient(),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=recency_days,
        batch_size=batch_size,
        floor=0.7,
    )


async def test_links_then_clusters_recent_pending(session):
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

    result = await _run(session, run_id)

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


async def test_rerun_is_noop_when_fully_processed(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await _run(session, run_id)
    assert result.linked == 0 and result.rejected == 0


async def test_link_window_of_four_includes_story_past_the_feed_window(session):
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

    result = await _run(session, run_id, recency_days=4)

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


class _TaggedFailClient(FakeClient):
    """Fails (or corrupts) the Stage-1 chunk whose payload contains a 'FAIL'-titled story."""

    def __init__(self, *, unparseable=False):
        super().__init__()
        self._unparseable = unparseable

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        if "entity-linking classifier" in system[0]["text"]:
            payload = json.loads(messages[0]["content"])
            if any(st["title"].startswith("FAIL") for st in payload["stories"]):
                if self._unparseable:
                    self.complete_calls.append(
                        {
                            "model": model,
                            "system": system,
                            "messages": messages,
                            "max_tokens": max_tokens,
                        }
                    )
                    return calls.record(CallResult(text="not json"))
                raise RuntimeError("boom")
        return await super().complete_call(
            model=model, system=system, messages=messages, max_tokens=max_tokens, calls=calls
        )


@pytest.mark.parametrize("unparseable", [False, True])
async def test_failed_chunk_stays_pending_others_commit(session, unparseable):
    """One chunk's failure — an API error or an unparseable reply — must not roll back or
    reject the chunks that succeeded. The failed chunk's stories stay `pending` so the next
    run retries them."""
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
        session, run_id, batch_size=1, client=_TaggedFailClient(unparseable=unparseable)
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
    assert run.items_failed == 1


# ---------------------------------------------------------------------------
# NEU-986: a total stage failure finalizes the run `failed`
# NEU-987: ...but a self-healing stage needs more than one failed candidate
# ---------------------------------------------------------------------------


class _TotalOutageClient(FakeClient):
    """Every call raises — a total LLM outage, so every chunk and every film fails."""

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        raise RuntimeError("boom")


async def test_single_link_failure_still_finalizes_run_failed(session):
    """NEU-987 relaxed the guard only on the *self-healing* stages. `link` is lossy — an
    unlinked story ages out of the recency window — so it keeps the strict rule and fails the
    run even when the whole backlog was one story."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add(await _story("https://e/only", published_offset_days=1))
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    await _run(session, run_id, batch_size=1, client=_TotalOutageClient())

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "failed"
    assert run.error and "link stage" in run.error


async def test_single_cluster_failure_does_not_fail_the_run(session):
    """NEU-987's core case: one pathological film on an otherwise empty backlog. Clustering
    is self-healing — the film stays unclustered and is re-selected next run — so failing the
    run here would abort the fail-fast daily chain *every day* and publish no summaries at
    all for as long as that one film stayed unclusterable."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await _linked_unclustered(session, film, "https://e/only-linked")
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    await _run(session, run_id, client=_TotalOutageClient())

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert run.error is None
    assert run.items_failed == 1  # still counted — only the terminal status is relaxed


async def test_total_link_failure_finalizes_run_failed(session):
    """When every link chunk fails there is no per-item isolation left to do: the stage
    produced nothing, so finalizing `succeeded` would let run_daily summarize unlinked
    stories and ping the deadman green (NEU-743's incident). The stories stay `pending`,
    the counters are still recorded, and the run ends `failed`."""
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

    result = await _run(session, run_id, batch_size=1, client=_TotalOutageClient())

    assert (result.linked, result.rejected) == (0, 0)
    rows = (
        (await session.execute(select(Story), execution_options={"populate_existing": True}))
        .scalars()
        .all()
    )
    assert all(s.link_status == "pending" for s in rows)  # untouched → a later run retries

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "failed"
    assert run.items_processed == 0
    assert run.items_failed == 2
    assert run.error and "link stage" in run.error


async def test_total_cluster_failure_finalizes_run_failed(session):
    """Stage 2 has the same degenerate case: nothing was linked this run (so the link stage
    is a legitimate no-op), every film failed to cluster, and a `succeeded` run would let the
    chain summarize and ping green off zero clustered events.

    Two films, because clustering is self-healing and so needs a denominator (NEU-987): with
    a backlog of one, `0 processed / 1 failed` is a bad film, not an outage."""
    films = [Film(tmdb_id=1, title="Runner"), Film(tmdb_id=2, title="Blade")]
    session.add_all(films)
    await session.flush()
    for i, film in enumerate(films):
        await _linked_unclustered(session, film, f"https://e/already-linked-{i}")
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    await _run(session, run_id, client=_TotalOutageClient())

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "failed"
    assert run.items_failed == 2
    assert run.error and "cluster stage" in run.error


async def test_run_with_nothing_to_do_still_succeeds(session):
    """Zero processed with zero failed is an idempotent no-op, not an outage — the guard
    must not fire on a run that simply had nothing pending."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    await _run(session, run_id, client=_TotalOutageClient())

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert run.error is None


# ---------------------------------------------------------------------------
# Request mapping (cache_control, max_tokens, model)
# ---------------------------------------------------------------------------


async def test_link_request_mapping(session):
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
    await _run(session, run_id, batch_size=1, client=client)

    calls = _link_calls(client)
    assert len(calls) == 2  # one call per chunk
    for c in calls:
        assert c["model"] == "claude-haiku-4-5"
        assert c["max_tokens"] == 2048  # == linker._MAX_TOKENS
        assert c["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_cluster_request_mapping(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all([await _story("https://e/a", published_offset_days=1)])
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    client = FakeClient()
    await _run(session, run_id, client=client)

    calls = _cluster_calls(client)
    assert len(calls) == 1  # one cluster call per film
    assert calls[0]["model"] == "claude-sonnet-4-6"
    assert calls[0]["max_tokens"] == 4096
    assert "cache_control" not in calls[0]["system"][0]
    assert "distinct EVENTS" in calls[0]["system"][0]["text"]


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
        cluster_max_tokens=7777,
    )

    assert all(c["max_tokens"] == 7777 for c in _cluster_calls(client))


# ---------------------------------------------------------------------------
# Cluster failure isolation
# ---------------------------------------------------------------------------


class _ClusterFailClient:
    """Stage-level fake: serves cluster calls, failing the film whose title starts 'FAIL'."""

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        payload = json.loads(messages[0]["content"])
        if payload["film"]["title"].startswith("FAIL"):
            raise RuntimeError("boom")
        new_ns = [s["n"] for s in payload["new_stories"]]
        return calls.record(
            CallResult(
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
                )
            )
        )


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


async def test_cluster_failure_is_isolated_per_film(session):
    ok_film = Film(tmdb_id=1, title="Runner")
    fail_film = Film(tmdb_id=2, title="FAIL Movie")
    session.add_all([ok_film, fail_film])
    await session.flush()
    ok_story = await _linked_unclustered(session, ok_film, "https://e/ok")
    fail_story = await _linked_unclustered(session, fail_film, "https://e/fail")
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    (
        events_created,
        stories_clustered,
        stories_rejected,
        _usage,
        _counts,
    ) = await _cluster_stage_sequential(
        session_factory=lambda: session,
        client=_ClusterFailClient(),
        run_id=run_id,
        model="claude-haiku-4-5",
        film_ids=[ok_film.id, fail_film.id],
        attach_limit=45,
        cluster_max_tokens=4096,
        run_date=date(2026, 1, 1),
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

    # NEU-987: the successful film is persisted too, not just the failed one — otherwise the
    # run row cannot reproduce the guard's decision.
    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert (run.items_processed, run.items_failed) == (1, 1)


async def test_cluster_stage_persists_processed_films(session):
    """NEU-987: a fully successful cluster stage must leave `items_processed` > 0. Before
    this, only `failed_delta` was written, so a clean stage persisted 0 processed / 0 failed
    — indistinguishable on the run row from a stage that never ran."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await _linked_unclustered(session, film, "https://e/ok")
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    await _cluster_stage_sequential(
        session_factory=lambda: session,
        client=FakeClient(),  # every film clusters cleanly
        run_id=run_id,
        model="claude-haiku-4-5",
        film_ids=[film.id],
        attach_limit=45,
        cluster_max_tokens=4096,
        run_date=date(2026, 1, 1),
    )

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert (run.items_processed, run.items_failed) == (1, 0)


# ---------------------------------------------------------------------------
# NEU-365: cluster parse failure surfaces as items_failed (not silent drop)
# ---------------------------------------------------------------------------


class _UnparseableClusterClient:
    """Returns an unparseable cluster response for every film, so apply_cluster_decisions
    raises ClusterParseError, which the pipeline catch-block records as failed_delta=1."""

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        return calls.record(CallResult(text="not json {"))


async def test_cluster_parse_failure_surfaces_as_failed(session):
    """A ClusterParseError increments items_failed on the run."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await _linked_unclustered(session, film, "https://e/1")
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    (
        events_created,
        stories_clustered,
        stories_rejected,
        _usage,
        _counts,
    ) = await _cluster_stage_sequential(
        session_factory=lambda: session,
        client=_UnparseableClusterClient(),
        run_id=run_id,
        model="claude-haiku-4-5",
        film_ids=[film.id],
        attach_limit=45,
        cluster_max_tokens=4096,
        run_date=date(2026, 1, 1),
    )

    assert events_created == 0
    assert stories_clustered == 0

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.items_failed == 1
