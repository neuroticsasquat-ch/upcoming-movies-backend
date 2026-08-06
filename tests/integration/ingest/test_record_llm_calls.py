"""`record_llm_calls` — the writer that turns a stage's `CallLog` into `ingest.llm_call`
rows (NEU-975)."""

from decimal import Decimal

from sqlalchemy import select

from upmovies.ingest import runs
from upmovies.ingest.models import LLMCall
from upmovies.llm.client import CallLog, CallResult, Usage
from upmovies.llm.pricing import HAIKU_4_5, price


async def _rows(session, run_id) -> list[LLMCall]:
    return list(
        (
            await session.execute(
                select(LLMCall).where(LLMCall.run_id == run_id).order_by(LLMCall.latency_ms),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


async def test_writes_one_row_per_recorded_call(session):
    run_id = await runs.create_run(session, kind="link")
    calls = CallLog()
    calls.record(CallResult(latency_ms=100))
    calls.record(CallResult(latency_ms=200))
    calls.record(CallResult(latency_ms=300))

    await runs.record_llm_calls(
        session, run_id, stage="link", model="claude-haiku-4-5", results=calls.results
    )
    await session.commit()

    assert [r.latency_ms for r in await _rows(session, run_id)] == [100, 200, 300]


async def test_records_usage_latency_attempts_and_parse_ok(session):
    run_id = await runs.create_run(session, kind="link")
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000,
        cache_creation_input_tokens=200,
    )
    calls = CallLog()
    calls.record(CallResult(text="{}", usage=usage, latency_ms=2500, attempts=2))
    calls.set_parse_ok(True)

    await runs.record_llm_calls(
        session, run_id, stage="cluster", model="claude-haiku-4-5", results=calls.results
    )
    await session.commit()

    (row,) = await _rows(session, run_id)
    assert row.stage == "cluster"
    assert row.model == "claude-haiku-4-5"
    assert row.input_tokens == 1_000_000
    assert row.output_tokens == 1_000_000
    assert row.cache_read_input_tokens == 1_000
    assert row.cache_creation_input_tokens == 200
    assert row.latency_ms == 2500
    assert row.attempts == 2
    assert row.ok is True
    assert row.error_type is None
    assert row.parse_ok is True
    # $1/Mtok in + $5/Mtok out + 1000 reads at 0.10x + 200 writes at 1.25x
    assert row.cost_usd == Decimal("6.000350")
    assert row.cost_usd == Decimal(str(price(usage, HAIKU_4_5, batch=False)))


async def test_provider_is_anthropic_until_the_gateway_lands(session):
    run_id = await runs.create_run(session, kind="link")
    calls = CallLog()
    calls.record(CallResult())

    await runs.record_llm_calls(
        session, run_id, stage="link", model="claude-haiku-4-5", results=calls.results
    )
    await session.commit()

    (row,) = await _rows(session, run_id)
    assert row.provider == "anthropic"


async def test_a_failed_call_still_writes_a_row_with_an_error_type(session):
    run_id = await runs.create_run(session, kind="link")
    calls = CallLog()
    calls.record(CallResult(latency_ms=42, attempts=3, ok=False, error_type="InternalServerError"))

    await runs.record_llm_calls(
        session, run_id, stage="link", model="claude-haiku-4-5", results=calls.results
    )
    await session.commit()

    (row,) = await _rows(session, run_id)
    assert row.ok is False
    assert row.error_type == "InternalServerError"
    assert row.attempts == 3
    assert row.parse_ok is None
    assert row.cost_usd == Decimal("0.000000")


async def test_no_calls_writes_nothing(session):
    run_id = await runs.create_run(session, kind="link")

    await runs.record_llm_calls(
        session, run_id, stage="link", model="claude-haiku-4-5", results=CallLog().results
    )
    await session.commit()

    assert await _rows(session, run_id) == []


async def test_per_call_tokens_sum_to_what_the_stage_aggregate_records(session):
    """The reconciliation NEU-975 asks for, made structural: both writes are fed by the same
    `CallLog`, so the per-call rows cannot drift from the per-stage `run_llm_usage` row."""
    run_id = await runs.create_run(session, kind="link")
    calls = CallLog()
    calls.record(CallResult(usage=Usage(input_tokens=10, output_tokens=1)))
    calls.record(CallResult(usage=Usage(input_tokens=20, output_tokens=2)))

    await runs.record_llm_calls(
        session, run_id, stage="link", model="claude-haiku-4-5", results=calls.results
    )
    await runs.record_llm_usage(
        session, run_id, stage="link", model="claude-haiku-4-5", usage=calls.usage
    )
    await session.commit()

    rows = await _rows(session, run_id)
    assert sum(r.input_tokens for r in rows) == 30
    assert sum(r.output_tokens for r in rows) == 3
    assert calls.usage == Usage(input_tokens=30, output_tokens=3)
