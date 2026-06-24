from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from upmovies.ingest.models import RunLLMUsage
from upmovies.ingest.runs import create_run


async def test_insert_and_read_back_a_usage_row(session):
    run_id = await create_run(session, kind="link")
    row = RunLLMUsage(
        run_id=run_id,
        stage="link",
        model="claude-haiku-4-5",
        batched=True,
        input_tokens=100,
        output_tokens=10,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=50,
        cost_usd=Decimal("0.001234"),
    )
    session.add(row)
    await session.commit()

    got = (
        await session.execute(select(RunLLMUsage).where(RunLLMUsage.run_id == run_id))
    ).scalar_one()
    assert got.stage == "link"
    assert got.model == "claude-haiku-4-5"
    assert got.batched is True
    assert got.input_tokens == 100
    assert got.cache_read_input_tokens == 900
    assert got.cost_usd == Decimal("0.001234")


async def test_token_columns_default_to_zero(session):
    run_id = await create_run(session, kind="link")
    row = RunLLMUsage(
        run_id=run_id,
        stage="cluster",
        model="claude-sonnet-4-6",
        batched=False,
        cost_usd=Decimal("0"),
    )
    session.add(row)
    await session.commit()
    got = (
        await session.execute(select(RunLLMUsage).where(RunLLMUsage.run_id == run_id))
    ).scalar_one()
    assert got.input_tokens == 0
    assert got.output_tokens == 0
    assert got.cache_read_input_tokens == 0
    assert got.cache_creation_input_tokens == 0


async def test_stage_check_constraint_rejects_bad_stage(session):
    run_id = await create_run(session, kind="link")
    session.add(
        RunLLMUsage(
            run_id=run_id,
            stage="bogus",
            model="m",
            batched=False,
            cost_usd=Decimal("0"),
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_unique_run_stage_rejects_duplicate(session):
    run_id = await create_run(session, kind="link")
    session.add(
        RunLLMUsage(run_id=run_id, stage="link", model="m", batched=False, cost_usd=Decimal("0"))
    )
    await session.commit()
    session.add(
        RunLLMUsage(run_id=run_id, stage="link", model="m", batched=False, cost_usd=Decimal("0"))
    )
    with pytest.raises(IntegrityError):
        await session.commit()
