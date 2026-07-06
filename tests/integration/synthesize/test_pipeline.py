import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import upmovies.synthesize.pipeline as pipeline_mod
from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run as _create_run
from upmovies.llm.client import BatchResult, Usage
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.synthesize.pipeline import _select_pending, _upsert_summary, run_synthesize_ingest
from upmovies.synthesize.summarizer import SummaryResult
from upmovies.synthesize.url_resolution import ResolveResult, mark_displayed_eligible


async def test_synthesize_is_a_valid_ingest_kind(session):
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()
    row = (await session.execute(select(IngestRun).where(IngestRun.id == run_id))).scalar_one()
    assert row.kind == "synthesize"


async def _film(session, *, tmdb_id=1, title="Runner"):
    film = Film(tmdb_id=tmdb_id, title=title)
    session.add(film)
    await session.flush()
    return film


async def _event_with_story(
    session, film, *, event_type="casting", dek="A dek.", source="Deadline", url=None
):
    story = Story(
        source=source,
        url=url or f"https://e/{film.id}-{event_type}",
        title="Headline",
        published_at=datetime.now(UTC),
        raw={"summary": dek},
    )
    session.add(story)
    await session.flush()
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="confirmed",
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.flush()
    return event


async def test_select_pending_includes_new_event_and_maps_input(session):
    film = await _film(session, title="Runner")
    event = await _event_with_story(session, film, event_type="trailer", dek="Trailer dropped.")
    await session.commit()

    pending = await _select_pending(session)

    assert len(pending) == 1
    pe = pending[0]
    assert pe.event_id == event.id
    assert pe.is_new is True
    assert pe.event_input.event_type == "trailer"
    assert pe.event_input.film_title == "Runner"
    assert pe.event_input.source_updated_at == event.updated_at
    assert [s.dek for s in pe.event_input.stories] == ["Trailer dropped."]
    assert [s.source for s in pe.event_input.stories] == ["Deadline"]


async def test_select_pending_skips_up_to_date_event(session):
    film = await _film(session)
    event = await _event_with_story(session, film)
    session.add(
        EventSummary(
            event_id=event.id,
            summary="done",
            model="m",
            prompt_version="1",
            source_updated_at=event.updated_at,
        )
    )
    await session.commit()

    pending = await _select_pending(session)
    assert pending == []


async def test_select_pending_skips_stale_event(session):
    film = await _film(session)
    event = await _event_with_story(session, film)
    # summary built against an OLDER updated_at than the event now has
    session.add(
        EventSummary(
            event_id=event.id,
            summary="old",
            model="m",
            prompt_version="1",
            source_updated_at=event.updated_at - timedelta(hours=1),
        )
    )
    await session.commit()

    pending = await _select_pending(session)
    assert pending == []


async def test_select_pending_skips_prompt_version_mismatch(session):
    film = await _film(session)
    event = await _event_with_story(session, film)
    session.add(
        EventSummary(
            event_id=event.id,
            summary="v1",
            model="m",
            prompt_version="1",
            source_updated_at=event.updated_at,
        )
    )
    await session.commit()

    pending = await _select_pending(session)
    assert pending == []


async def test_upsert_summary_inserts_then_updates_idempotently(session):
    film = await _film(session)
    event = await _event_with_story(session, film)
    await session.commit()

    first = SummaryResult(
        summary="First.",
        model="claude-haiku-4-5",
        prompt_version="1",
        source_updated_at=event.updated_at,
    )
    await _upsert_summary(session, event.id, first)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(EventSummary).where(EventSummary.event_id == event.id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].summary == "First."

    second = SummaryResult(
        summary="Second.",
        model="claude-haiku-4-5",
        prompt_version="2",
        source_updated_at=event.updated_at,
    )
    await _upsert_summary(session, event.id, second)
    await session.commit()

    row = (
        await session.execute(
            select(EventSummary).where(EventSummary.event_id == event.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.summary == "Second."
    assert row.prompt_version == "2"


class FakeSummaryClient:
    """Serves both surfaces. `complete_with_usage` (sequential) and `complete_batch` (batched)
    both return the JSON summary envelope the service expects."""

    def __init__(self, summary="A neutral update."):
        self._summary = summary
        self.complete_calls = 0
        self.batch_requests = None

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        self.complete_calls += 1
        return json.dumps({"summary": self._summary}), Usage()

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        self.batch_requests = list(requests)
        return {
            r.custom_id: BatchResult(
                custom_id=r.custom_id,
                ok=True,
                text=json.dumps({"summary": self._summary}),
                usage=Usage(),
            )
            for r in self.batch_requests
        }


async def _run(session, run_id, *, use_batches=False, client=None, prompt_version="1"):
    return await run_synthesize_ingest(
        session_factory=lambda: session,
        client=client or FakeSummaryClient(),
        run_id=run_id,
        model="claude-haiku-4-5",
        prompt_version=prompt_version,
        use_batches=use_batches,
    )


async def test_sequential_summarizes_new_event_and_finalizes(session):
    film = await _film(session)
    event = await _event_with_story(session, film, dek="Star cast.")
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    result = await _run(session, run_id, use_batches=False)

    assert (result.new, result.refreshed, result.failed) == (1, 0, 0)
    row = (
        await session.execute(
            select(EventSummary).where(EventSummary.event_id == event.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.summary == "A neutral update."
    assert row.source_updated_at == event.updated_at

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert (
        run.detail == "summarized 1 (1 new, 0 refreshed); 0 failed; "
        "urls marked 0, resolved 0, failed 0, pending 0"
    )


async def test_rerun_is_noop_when_nothing_pending(session):
    film = await _film(session)
    await _event_with_story(session, film)
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()
    await _run(session, run_id, use_batches=False)  # first run summarizes

    run_id2 = await _create_run(session, kind="synthesize")
    await session.commit()
    result = await _run(session, run_id2, use_batches=False)

    assert (result.new, result.refreshed, result.failed) == (0, 0, 0)


async def test_prompt_version_bump_does_not_refresh_existing(session):
    film = await _film(session)
    event = await _event_with_story(session, film)
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()
    await _run(session, run_id, use_batches=False, prompt_version="1")

    run_id2 = await _create_run(session, kind="synthesize")
    await session.commit()
    result = await _run(session, run_id2, use_batches=False, prompt_version="2")

    assert (result.new, result.refreshed, result.failed) == (0, 0, 0)
    row = (
        await session.execute(
            select(EventSummary).where(EventSummary.event_id == event.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.prompt_version == "1"


class _FailOneCompleter(FakeSummaryClient):
    """Raises for the event whose film title starts 'FAIL', succeeds otherwise."""

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        payload = json.loads(messages[0]["content"])
        if payload["film"].startswith("FAIL"):
            raise RuntimeError("boom")
        return json.dumps({"summary": self._summary}), Usage()


async def test_sequential_failure_is_isolated_per_event(session):
    ok_film = await _film(session, tmdb_id=1, title="Runner")
    fail_film = await _film(session, tmdb_id=2, title="FAIL Movie")
    ok_event = await _event_with_story(session, ok_film)
    fail_event = await _event_with_story(session, fail_film)
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    result = await _run(session, run_id, use_batches=False, client=_FailOneCompleter())

    assert (result.new, result.failed) == (1, 1)
    summarized = {
        r.event_id
        for r in (
            await session.execute(
                select(EventSummary), execution_options={"populate_existing": True}
            )
        )
        .scalars()
        .all()
    }
    assert ok_event.id in summarized
    assert fail_event.id not in summarized

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert run.items_processed == 1
    assert run.items_failed == 1


@pytest.mark.parametrize("use_batches", [False, True])
async def test_both_paths_summarize_new_event(session, use_batches):
    film = await _film(session)
    event = await _event_with_story(session, film)
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    result = await _run(session, run_id, use_batches=use_batches)

    assert (result.new, result.refreshed, result.failed) == (1, 0, 0)
    row = (
        await session.execute(
            select(EventSummary).where(EventSummary.event_id == event.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.summary == "A neutral update."


async def test_batched_request_mapping(session):
    film = await _film(session)
    event = await _event_with_story(session, film)
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    client = FakeSummaryClient()
    await _run(session, run_id, use_batches=True, client=client)

    reqs = client.batch_requests
    assert reqs is not None
    assert {r.custom_id for r in reqs} == {str(event.id)}  # one request per event, keyed by id
    assert reqs[0].model == "claude-haiku-4-5"
    assert reqs[0].max_tokens == 256
    assert client.complete_calls == 0  # batched path never calls complete()


class _BatchFailOne:
    """Fails the request whose event payload's film starts 'FAIL'; succeeds otherwise."""

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        out = {}
        for r in requests:
            payload = json.loads(r.messages[0]["content"])
            if payload["film"].startswith("FAIL"):
                out[r.custom_id] = BatchResult(
                    custom_id=r.custom_id, ok=False, error_type="errored", error_message="boom"
                )
            else:
                out[r.custom_id] = BatchResult(
                    custom_id=r.custom_id,
                    ok=True,
                    text=json.dumps({"summary": "ok."}),
                    usage=Usage(),
                )
        return out


async def test_batched_failure_is_isolated_per_event(session):
    ok_film = await _film(session, tmdb_id=1, title="Runner")
    fail_film = await _film(session, tmdb_id=2, title="FAIL Movie")
    ok_event = await _event_with_story(session, ok_film)
    fail_event = await _event_with_story(session, fail_film)
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    result = await _run(session, run_id, use_batches=True, client=_BatchFailOne())

    assert (result.new, result.failed) == (1, 1)
    summarized = {
        r.event_id
        for r in (
            await session.execute(
                select(EventSummary), execution_options={"populate_existing": True}
            )
        )
        .scalars()
        .all()
    }
    assert ok_event.id in summarized
    assert fail_event.id not in summarized


class _RaisingBatchClient:
    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        raise TimeoutError("batch never reached 'ended'")


async def test_batched_whole_submit_failure_marks_all_failed_run_succeeds(session):
    film = await _film(session)
    await _event_with_story(session, film)
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    result = await _run(session, run_id, use_batches=True, client=_RaisingBatchClient())

    assert (result.new, result.refreshed, result.failed) == (0, 0, 1)
    rows = (
        (await session.execute(select(EventSummary), execution_options={"populate_existing": True}))
        .scalars()
        .all()
    )
    assert rows == []  # nothing summarized; events retried next run

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"


async def test_synthesize_invokes_url_resolution_for_run_events(session, monkeypatch):
    film = await _film(session)
    event = await _event_with_story(
        session, film, url="https://news.google.com/rss/articles/CBMiwire"
    )
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    called = {}

    async def fake_resolution(*, session_factory, **kwargs):
        called["invoked"] = True
        async with session_factory() as s:
            await mark_displayed_eligible(s)
            await s.commit()
        return ResolveResult(marked=1, resolved=0, failed=0, pending=1)

    monkeypatch.setattr(pipeline_mod, "run_url_resolution", fake_resolution)

    await _run(session, run_id)

    assert called.get("invoked")
    story = (
        await session.execute(
            select(Story)
            .join(EventStory, EventStory.story_id == Story.id)
            .where(EventStory.event_id == event.id)
        )
    ).scalar_one()
    await session.refresh(story)
    assert story.resolve_state == "pending"


async def test_synthesize_resolution_crash_keeps_summary(session, monkeypatch):
    film = await _film(session)
    event = await _event_with_story(session, film, dek="Star cast.")
    await session.commit()
    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    async def boom(*, session_factory, **kwargs):
        raise RuntimeError("decode blew up")

    monkeypatch.setattr(pipeline_mod, "run_url_resolution", boom)

    result = await _run(session, run_id)

    assert result.new == 1  # summary still produced despite the resolution crash
    summary = (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one_or_none()
    assert summary is not None

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"  # resolution crash did not fail the run


async def test_detail_reports_marked_google_news_story(session):
    film = await _film(session)
    event = Event(
        film_id=film.id,
        event_type="casting",
        confidence="confirmed",
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    story = Story(
        source="Google News: per-film",
        url="https://news.google.com/rss/articles/CBMipipe",
        title="Star cast.",
        published_at=datetime.now(UTC),
    )
    session.add(story)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.commit()

    run_id = await _create_run(session, kind="synthesize")
    await session.commit()

    async def fake_resolver(client, url):
        return "https://variety.com/real"

    await run_synthesize_ingest(
        session_factory=lambda: session,
        client=FakeSummaryClient(),
        run_id=run_id,
        model="claude-haiku-4-5",
        prompt_version="1",
        use_batches=False,
        url_resolve_delay_seconds=0.0,
        url_resolve_resolver=fake_resolver,
    )

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert "urls marked 1, resolved 1" in run.detail
