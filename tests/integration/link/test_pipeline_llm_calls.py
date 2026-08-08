"""Per-call telemetry from a `link` run (NEU-975): every LLM call the pipeline makes leaves
exactly one `ingest.llm_call` row, failures included, reconciling with `run_llm_usage`."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tests.fixtures.gateway import StubGateway
from upmovies.catalog.models import Film
from upmovies.ingest.models import LLMCall, RunLLMUsage
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm import CallResult, Usage
from upmovies.news.models import SourceDomain, Story

_USAGE = Usage(input_tokens=100, output_tokens=10, cache_read_input_tokens=900)


class _FakeClient:
    """Answers all three `link`-run call sites, routing on the prompt the way the production
    fakes do. `fail_titles` makes the link call for a batch containing that title blow up."""

    def __init__(self, *, fail_titles: frozenset[str] = frozenset()):
        self._fail_titles = fail_titles
        self.n_calls = 0

    async def complete_call(self, *, model, prompt, calls):
        self.n_calls += 1
        instructions = prompt.stable_prefix
        if "source-quality rater" in instructions:
            items = json.loads(prompt.user)
            return calls.record(
                CallResult(
                    text=json.dumps(
                        [
                            {"domain": i["domain"], "tier": "acceptable", "reason": "ok"}
                            for i in items
                        ]
                    ),
                    usage=_USAGE,
                    latency_ms=7,
                )
            )
        if "entity-linking classifier" in instructions:
            stories = json.loads(prompt.user)["stories"]
            if any(s["title"] in self._fail_titles for s in stories):
                # Recorded then raised — exactly what AnthropicClient does on a provider error.
                calls.record(
                    CallResult(latency_ms=5, attempts=2, ok=False, error_type="APIStatusError")
                )
                raise RuntimeError("boom")
            return calls.record(
                CallResult(
                    text=json.dumps(
                        [
                            {"id": s["id"], "film": 1, "confidence": 0.95, "reason": "about"}
                            for s in stories
                        ]
                    ),
                    usage=_USAGE,
                    latency_ms=11,
                )
            )
        new_ns = [s["n"] for s in json.loads(prompt.user)["new_stories"]]
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
                ),
                usage=_USAGE,
                latency_ms=13,
            )
        )


async def _story(session, url, *, title="Runner news"):
    s = Story(
        source="X",
        url=url,
        title=title,
        published_at=datetime.now(UTC) - timedelta(days=1),
        link_status="pending",
        raw={"summary": ""},
    )
    session.add(s)
    await session.flush()
    return s


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


async def _run(session, client, **kwargs):
    run_id = await create_run(session, kind="link")
    await session.commit()
    await run_link_ingest(
        session_factory=lambda: session,
        gateway=StubGateway(client),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=45,
        batch_size=10,
        floor=0.7,
        **kwargs,
    )
    return run_id


async def test_every_call_in_a_link_run_writes_exactly_one_row(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await _story(session, "https://variety.com/1")
    await session.commit()

    client = _FakeClient()
    run_id = await _run(session, client, source_gate_enabled=True)

    rows = await _calls(session, run_id)
    assert len(rows) == client.n_calls
    # One call each: link the batch, judge the unknown domain, cluster the film.
    assert sorted(r.stage for r in rows) == ["cluster", "link", "source_judge"]
    assert {r.provider for r in rows} == {"anthropic"}
    assert {r.ok for r in rows} == {True}
    assert {r.parse_ok for r in rows} == {True}
    assert {r.attempts for r in rows} == {1}
    by_stage = {r.stage: r for r in rows}
    assert by_stage["link"].model == "claude-haiku-4-5"
    assert by_stage["cluster"].model == "claude-sonnet-4-6"
    assert by_stage["link"].latency_ms == 11
    assert by_stage["cluster"].latency_ms == 13
    assert by_stage["source_judge"].latency_ms == 7


async def test_per_call_tokens_reconcile_with_the_stage_aggregate(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    # Two batches of one story each, so `link` makes two calls that must sum to one aggregate.
    await _story(session, "https://variety.com/1")
    await _story(session, "https://variety.com/2")
    session.add(
        SourceDomain(
            domain="variety.com",
            admin_override="none",
            first_seen_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()

    run_id = await create_run(session, kind="link")
    await session.commit()
    await run_link_ingest(
        session_factory=lambda: session,
        gateway=StubGateway(_FakeClient()),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=45,
        batch_size=1,
        floor=0.7,
    )

    rows = await _calls(session, run_id)
    aggregates = {
        r.stage: r
        for r in (
            await session.execute(
                select(RunLLMUsage).where(RunLLMUsage.run_id == run_id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    }
    assert len([r for r in rows if r.stage == "link"]) == 2
    for stage, aggregate in aggregates.items():
        per_call = [r for r in rows if r.stage == stage]
        assert sum(r.input_tokens for r in per_call) == aggregate.input_tokens
        assert sum(r.output_tokens for r in per_call) == aggregate.output_tokens
        assert sum(r.cache_read_input_tokens for r in per_call) == aggregate.cache_read_input_tokens


async def test_a_failed_call_still_writes_a_row(session):
    """The failure isolation is unchanged — the batch is still counted failed — but the call
    that burned the latency is no longer invisible."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    # "Runner" so retrieval offers a candidate and the call is actually made.
    await _story(session, "https://variety.com/1", title="FAIL me Runner")
    await session.commit()

    run_id = await _run(session, _FakeClient(fail_titles=frozenset({"FAIL me Runner"})))

    (row,) = [r for r in await _calls(session, run_id) if r.stage == "link"]
    assert row.ok is False
    assert row.error_type == "APIStatusError"
    assert row.attempts == 2
    assert row.latency_ms == 5
    assert row.parse_ok is None  # never got as far as a parse
