from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from upmovies.ingest.models import RunLLMUsage
from upmovies.ingest.runs import create_run
from upmovies.llm import Usage
from upmovies.llm.pricing import price, rates_for


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


async def test_historical_batched_row_still_prices_at_the_batch_discount(session):
    """ADR-0005 removed the Message Batches call path but deliberately kept the `batched`
    column and `price(..., batch=)`. Rows written before the removal must stay queryable AND
    keep re-pricing at the 50% discount they were charged at — the column is the only record
    of which rate applied."""
    run_id = await create_run(session, kind="link")
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    session.add(
        RunLLMUsage(
            run_id=run_id,
            stage="link",
            model="claude-haiku-4-5",
            batched=True,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=Decimal(
                str(price(usage, rates_for("anthropic", "claude-haiku-4-5"), batch=True))
            ),
        )
    )
    await session.commit()

    got = (
        await session.execute(select(RunLLMUsage).where(RunLLMUsage.run_id == run_id))
    ).scalar_one()
    reconstructed = Usage(input_tokens=got.input_tokens, output_tokens=got.output_tokens)
    # Re-pricing the stored row off its own `batched` flag reproduces the stored cost:
    # $6.00 at full rate, halved to $3.00 by the batch discount. `run_llm_usage` carries no
    # provider column — every row it holds predates the gateway, so they are all Anthropic.
    assert float(got.cost_usd) == price(
        reconstructed, rates_for("anthropic", got.model), batch=got.batched
    )
    assert got.cost_usd == Decimal("3.000000")


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
