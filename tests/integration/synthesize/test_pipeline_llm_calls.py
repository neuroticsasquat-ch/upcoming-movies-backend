"""Per-call telemetry from a `synthesize` run (NEU-975)."""

import json
from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import LLMCall, RunLLMUsage
from upmovies.ingest.runs import create_run
from upmovies.llm import CallResult, Usage
from upmovies.news.models import Event, EventStory, Story
from upmovies.synthesize.pipeline import run_synthesize_ingest

_USAGE = Usage(input_tokens=80, output_tokens=12)


class _FakeClient:
    """Summarizes every event; `fail_films` makes the call raise, `unparseable_films` makes it
    return output `parse_summary` chokes on."""

    def __init__(
        self,
        *,
        fail_films: frozenset[str] = frozenset(),
        unparseable_films: frozenset[str] = frozenset(),
    ):
        self._fail_films = fail_films
        self._unparseable_films = unparseable_films

    async def complete_call(self, *, model, prompt, calls):
        film = json.loads(prompt.user)["film"]
        if film in self._fail_films:
            calls.record(
                CallResult(latency_ms=3, attempts=3, ok=False, error_type="APITimeoutError")
            )
            raise RuntimeError("boom")
        text = (
            "not a summary envelope"
            if film in self._unparseable_films
            else json.dumps({"summary": "A neutral update."})
        )
        return calls.record(CallResult(text=text, usage=_USAGE, latency_ms=9))


async def _event_for(session, *, tmdb_id, title):
    film = Film(tmdb_id=tmdb_id, title=title)
    session.add(film)
    await session.flush()
    story = Story(
        source="Deadline",
        url=f"https://e/{tmdb_id}",
        title="Headline",
        published_at=datetime.now(UTC),
        raw={"summary": "A dek."},
    )
    session.add(story)
    await session.flush()
    event = Event(
        film_id=film.id,
        event_type="trailer",
        confidence="confirmed",
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.flush()
    return event


async def _run(session, client):
    run_id = await create_run(session, kind="synthesize")
    await session.commit()
    await run_synthesize_ingest(
        session_factory=lambda: session,
        client=client,
        run_id=run_id,
        model="claude-haiku-4-5",
        prompt_version="1",
    )
    return run_id


async def _calls(session, run_id) -> list[LLMCall]:
    return list(
        (
            await session.execute(
                select(LLMCall).where(LLMCall.run_id == run_id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


async def test_each_summarized_event_writes_one_row(session):
    await _event_for(session, tmdb_id=1, title="Runner")
    await _event_for(session, tmdb_id=2, title="Blade")
    await session.commit()

    run_id = await _run(session, _FakeClient())

    rows = await _calls(session, run_id)
    assert len(rows) == 2
    assert {r.stage for r in rows} == {"summarize"}
    assert {r.model for r in rows} == {"claude-haiku-4-5"}
    assert {r.ok for r in rows} == {True}
    assert {r.parse_ok for r in rows} == {True}
    assert {r.latency_ms for r in rows} == {9}

    aggregate = (
        await session.execute(
            select(RunLLMUsage).where(RunLLMUsage.run_id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert sum(r.input_tokens for r in rows) == aggregate.input_tokens
    assert sum(r.output_tokens for r in rows) == aggregate.output_tokens


async def test_an_unparseable_reply_records_parse_ok_false_and_still_costs(session):
    """`summarize` catches and skips the event, but the call was paid for either way — which
    is precisely the outcome the per-call grain exists to keep visible."""
    await _event_for(session, tmdb_id=1, title="Runner")
    await _event_for(session, tmdb_id=2, title="Blade")
    await session.commit()

    run_id = await _run(session, _FakeClient(unparseable_films=frozenset({"Blade"})))

    rows = {r.parse_ok: r for r in await _calls(session, run_id)}
    assert set(rows) == {True, False}
    assert rows[False].ok is True  # the API call itself succeeded
    assert rows[False].input_tokens == _USAGE.input_tokens


async def test_a_failed_call_writes_a_row_with_an_error_type(session):
    await _event_for(session, tmdb_id=1, title="Runner")
    await _event_for(session, tmdb_id=2, title="Blade")
    await session.commit()

    run_id = await _run(session, _FakeClient(fail_films=frozenset({"Blade"})))

    failed = [r for r in await _calls(session, run_id) if not r.ok]
    assert len(failed) == 1
    assert failed[0].error_type == "APITimeoutError"
    assert failed[0].attempts == 3
    assert failed[0].parse_ok is None
