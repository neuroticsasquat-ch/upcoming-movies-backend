"""`ingest.llm_call` — one row per logical LLM API call (NEU-974).

The per-`(run, stage)` grain of `ingest.run_llm_usage` structurally cannot express the two
properties provider selection turns on: latency is not summable and cache-hit is not
averageable. These tests pin the per-call grain's constraints, not its writers — nothing
writes `llm_call` yet.
"""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from upmovies.ingest.models import IngestRun, LLMCall
from upmovies.ingest.runs import create_run


def _call(run_id: UUID, **overrides) -> LLMCall:
    fields: dict = {
        "run_id": run_id,
        "stage": "link",
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "latency_ms": 1234,
        "ok": True,
        "cost_usd": Decimal("0"),
    }
    fields.update(overrides)
    return LLMCall(**fields)


async def test_insert_and_read_back_a_call_row(session):
    run_id = await create_run(session, kind="link")
    session.add(
        _call(
            run_id,
            input_tokens=100,
            output_tokens=10,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=50,
            latency_ms=2500,
            attempts=2,
            parse_ok=True,
            cost_usd=Decimal("0.001234"),
        )
    )
    await session.commit()

    got = (await session.execute(select(LLMCall).where(LLMCall.run_id == run_id))).scalar_one()
    assert got.stage == "link"
    assert got.provider == "anthropic"
    assert got.model == "claude-haiku-4-5"
    assert got.input_tokens == 100
    assert got.output_tokens == 10
    assert got.cache_read_input_tokens == 900
    assert got.cache_creation_input_tokens == 50
    assert got.latency_ms == 2500
    assert got.attempts == 2
    assert got.ok is True
    assert got.error_type is None
    assert got.parse_ok is True
    assert got.cost_usd == Decimal("0.001234")
    assert got.created_at is not None


async def test_token_columns_default_to_zero_and_attempts_to_one(session):
    run_id = await create_run(session, kind="link")
    session.add(_call(run_id))
    await session.commit()

    got = (await session.execute(select(LLMCall).where(LLMCall.run_id == run_id))).scalar_one()
    assert got.input_tokens == 0
    assert got.output_tokens == 0
    assert got.cache_read_input_tokens == 0
    assert got.cache_creation_input_tokens == 0
    assert got.attempts == 1


async def test_parse_ok_is_null_when_the_caller_performs_no_json_parse(session):
    """`parse_ok` is three-valued on purpose: True/False for a caller that parses JSON,
    NULL for one that doesn't — which is not the same as a parse that failed."""
    run_id = await create_run(session, kind="link")
    session.add(_call(run_id))
    await session.commit()

    got = (await session.execute(select(LLMCall).where(LLMCall.run_id == run_id))).scalar_one()
    assert got.parse_ok is None


async def test_a_failed_call_records_its_error_type(session):
    run_id = await create_run(session, kind="link")
    session.add(_call(run_id, ok=False, error_type="overloaded_error", attempts=3, parse_ok=False))
    await session.commit()

    got = (await session.execute(select(LLMCall).where(LLMCall.run_id == run_id))).scalar_one()
    assert got.ok is False
    assert got.error_type == "overloaded_error"
    assert got.attempts == 3
    assert got.parse_ok is False


@pytest.mark.parametrize("stage", ["link", "cluster", "summarize", "source_judge"])
async def test_stage_check_constraint_accepts_the_four_known_stages(session, stage):
    run_id = await create_run(session, kind="link")
    session.add(_call(run_id, stage=stage))
    await session.commit()

    got = (await session.execute(select(LLMCall).where(LLMCall.run_id == run_id))).scalar_one()
    assert got.stage == stage


async def test_stage_check_constraint_rejects_bad_stage(session):
    run_id = await create_run(session, kind="link")
    session.add(_call(run_id, stage="bogus"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_attempts_check_constraint_rejects_less_than_one(session):
    """A logical call is at least one attempt; 0 would make retry rates unreadable."""
    run_id = await create_run(session, kind="link")
    session.add(_call(run_id, attempts=0))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_deleting_the_run_cascades_to_its_calls(session):
    """Core-level delete, so this exercises the FK's ON DELETE CASCADE rather than
    SQLAlchemy's in-Python delete-orphan cascade."""
    run_id = await create_run(session, kind="link")
    session.add_all([_call(run_id, stage="link"), _call(run_id, stage="cluster")])
    await session.commit()

    await session.execute(delete(IngestRun).where(IngestRun.id == run_id))
    await session.commit()

    remaining = (
        (await session.execute(select(LLMCall).where(LLMCall.run_id == run_id))).scalars().all()
    )
    assert remaining == []


async def test_many_calls_per_run_and_stage(session):
    """Unlike `run_llm_usage`, the per-call grain has no unique (run, stage) constraint —
    that is the entire point: p50/p95 latency needs every call, not one aggregate row."""
    run_id = await create_run(session, kind="link")
    session.add_all([_call(run_id, stage="link", latency_ms=ms) for ms in (100, 200, 300)])
    await session.commit()

    rows = (await session.execute(select(LLMCall).where(LLMCall.stage == "link"))).scalars().all()
    assert sorted(r.latency_ms for r in rows) == [100, 200, 300]
