import json
from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import RunLLMUsage
from upmovies.ingest.runs import create_run
from upmovies.llm.client import BatchResult, Usage
from upmovies.news.models import Event, EventStory, Story
from upmovies.synthesize.pipeline import run_synthesize_ingest


class UsageSummaryClient:
    def __init__(self, usage: Usage):
        self._usage = usage

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        return json.dumps({"summary": "A neutral update."}), self._usage

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        return {
            r.custom_id: BatchResult(
                custom_id=r.custom_id,
                ok=True,
                text=json.dumps({"summary": "A neutral update."}),
                usage=self._usage,
            )
            for r in requests
        }


async def _event(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    story = Story(
        source="Deadline",
        url="https://e/1",
        title="Headline",
        published_at=datetime.now(UTC),
        raw={"summary": "A dek."},
    )
    session.add(story)
    await session.flush()
    event = Event(
        film_id=film.id, event_type="casting", confidence="confirmed", occurred_at=datetime.now(UTC)
    )
    session.add(event)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.flush()
    return event


async def test_run_synthesize_records_summarize_usage_sequential(session):
    await _event(session)
    await session.commit()
    run_id = await create_run(session, kind="synthesize")
    await session.commit()

    await run_synthesize_ingest(
        session_factory=lambda: session,
        client=UsageSummaryClient(Usage(input_tokens=42, output_tokens=8)),
        run_id=run_id,
        model="claude-haiku-4-5",
        prompt_version="1",
        use_batches=False,
    )

    row = (
        await session.execute(
            select(RunLLMUsage).where(RunLLMUsage.run_id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.stage == "summarize"
    assert row.model == "claude-haiku-4-5"
    assert row.batched is False
    assert row.input_tokens == 42
    assert row.output_tokens == 8


async def test_run_synthesize_records_summarize_usage_batched(session):
    await _event(session)
    await session.commit()
    run_id = await create_run(session, kind="synthesize")
    await session.commit()

    await run_synthesize_ingest(
        session_factory=lambda: session,
        client=UsageSummaryClient(Usage(input_tokens=5)),
        run_id=run_id,
        model="claude-haiku-4-5",
        prompt_version="1",
        use_batches=True,
    )

    row = (
        await session.execute(select(RunLLMUsage).where(RunLLMUsage.run_id == run_id))
    ).scalar_one()
    assert row.stage == "summarize"
    assert row.batched is True
    assert row.input_tokens == 5
