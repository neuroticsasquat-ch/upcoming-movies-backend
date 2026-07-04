from datetime import UTC, datetime

from upmovies.news.source_quality import (
    get_source_domains,
    list_source_domains,
    set_override,
    upsert_judgements,
)


async def test_upsert_and_get(session):
    now = datetime.now(UTC)
    n = await upsert_judgements(
        session,
        {"mshale.com": ("low", "aggregator"), "variety.com": ("trusted", "trade")},
        model="claude-haiku-4-5",
        now=now,
    )
    await session.commit()
    assert n == 2
    rows = await get_source_domains(session, ["mshale.com", "variety.com", "unknown.test"])
    assert set(rows) == {"mshale.com", "variety.com"}
    assert rows["mshale.com"].llm_tier == "low"
    assert rows["variety.com"].llm_model == "claude-haiku-4-5"
    assert rows["mshale.com"].judged_at is not None


async def test_get_empty_returns_empty(session):
    assert await get_source_domains(session, []) == {}


async def test_set_override_creates_then_updates(session):
    now = datetime.now(UTC)
    row = await set_override(session, domain="Mshale.com", override="block", now=now)
    await session.commit()
    assert row.domain == "mshale.com"  # normalized to lowercase
    assert row.admin_override == "block"
    assert row.llm_tier is None

    row2 = await set_override(session, domain="mshale.com", override="trust", now=now)
    await session.commit()
    assert row2.admin_override == "trust"
    all_rows = await list_source_domains(session)
    assert len(all_rows) == 1


async def test_set_override_preserves_llm_tier(session):
    now = datetime.now(UTC)
    await upsert_judgements(session, {"deadline.com": ("trusted", "trade")}, model="m", now=now)
    await session.commit()
    row = await set_override(session, domain="deadline.com", override="block", now=now)
    await session.commit()
    assert row.llm_tier == "trusted"
    assert row.admin_override == "block"
