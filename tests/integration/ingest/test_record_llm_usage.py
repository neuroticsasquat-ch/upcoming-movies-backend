from decimal import Decimal

from sqlalchemy import select

from upmovies.ingest import runs
from upmovies.ingest.models import RunLLMUsage
from upmovies.llm.client import Usage
from upmovies.llm.pricing import HAIKU_4_5, price


async def test_record_llm_usage_inserts_priced_row(session):
    run_id = await runs.create_run(session, kind="link")
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    await runs.record_llm_usage(
        session, run_id, stage="link", model="claude-haiku-4-5", batched=False, usage=usage
    )
    await session.commit()

    row = (
        await session.execute(
            select(RunLLMUsage).where(RunLLMUsage.run_id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.stage == "link"
    assert row.model == "claude-haiku-4-5"
    assert row.batched is False
    assert row.input_tokens == 1_000_000
    assert row.output_tokens == 1_000_000
    # full-rate price: $1/Mtok in + $5/Mtok out = $6.000000
    assert row.cost_usd == Decimal("6.000000")
    assert float(row.cost_usd) == price(usage, HAIKU_4_5, batch=False)


async def test_record_llm_usage_applies_batch_discount(session):
    run_id = await runs.create_run(session, kind="link")
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    await runs.record_llm_usage(
        session, run_id, stage="link", model="claude-haiku-4-5", batched=True, usage=usage
    )
    await session.commit()
    row = (
        await session.execute(select(RunLLMUsage).where(RunLLMUsage.run_id == run_id))
    ).scalar_one()
    # batch halves the $6.00 → $3.00
    assert row.cost_usd == Decimal("3.000000")


async def test_record_llm_usage_upserts_on_run_stage(session):
    run_id = await runs.create_run(session, kind="link")
    await runs.record_llm_usage(
        session,
        run_id,
        stage="link",
        model="claude-haiku-4-5",
        batched=False,
        usage=Usage(input_tokens=10),
    )
    await session.commit()
    # second call for the same (run, stage) overwrites, not duplicates
    await runs.record_llm_usage(
        session,
        run_id,
        stage="link",
        model="claude-haiku-4-5",
        batched=False,
        usage=Usage(input_tokens=99),
    )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(RunLLMUsage).where(RunLLMUsage.run_id == run_id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].input_tokens == 99


async def test_record_llm_usage_two_stages_two_rows(session):
    run_id = await runs.create_run(session, kind="link")
    await runs.record_llm_usage(
        session, run_id, stage="link", model="claude-haiku-4-5", batched=False, usage=Usage()
    )
    await runs.record_llm_usage(
        session, run_id, stage="cluster", model="claude-sonnet-4-6", batched=False, usage=Usage()
    )
    await session.commit()
    rows = (
        (await session.execute(select(RunLLMUsage).where(RunLLMUsage.run_id == run_id)))
        .scalars()
        .all()
    )
    assert {r.stage for r in rows} == {"link", "cluster"}
