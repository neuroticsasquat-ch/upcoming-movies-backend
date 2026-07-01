from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from upmovies.news.models import SourceDomain


async def test_insert_and_read_source_domain(session):
    now = datetime.now(UTC)
    session.add(
        SourceDomain(
            domain="mshale.com",
            llm_tier="low",
            llm_reason="low-quality aggregator",
            llm_model="claude-haiku-4-5",
            admin_override="none",
            first_seen_at=now,
            judged_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    row = (
        await session.execute(select(SourceDomain).where(SourceDomain.domain == "mshale.com"))
    ).scalar_one()
    assert row.llm_tier == "low"
    assert row.admin_override == "none"


async def test_tier_check_constraint_rejects_bad_value(session):
    now = datetime.now(UTC)
    session.add(
        SourceDomain(
            domain="bad.test",
            llm_tier="great",
            admin_override="none",
            first_seen_at=now,
            updated_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_override_check_constraint_rejects_bad_value(session):
    now = datetime.now(UTC)
    session.add(
        SourceDomain(
            domain="bad2.test",
            admin_override="banish",
            first_seen_at=now,
            updated_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
