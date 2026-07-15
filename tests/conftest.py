import os

os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ.pop("COOKIE_DOMAIN", None)

pytest_plugins = ["tests.fixtures.users", "tests.fixtures.public"]

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import upmovies.models  # noqa: F401, E402  -- register every model with Base.metadata
from upmovies.db import Base  # noqa: E402

_SCHEMAS = ("app", "catalog", "news", "ingest")


@pytest.fixture(scope="session")
async def test_engine():
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.begin() as conn:
        for s in _SCHEMAS:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE"))
        await conn.execute(text("CREATE SCHEMA app"))
        await conn.execute(text("CREATE SCHEMA catalog"))
        await conn.execute(text("CREATE SCHEMA news"))
        await conn.execute(text("CREATE SCHEMA ingest"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        for s in _SCHEMAS:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE"))
    await engine.dispose()


@pytest.fixture(scope="session")
def session_factory(test_engine):
    """Async sessionmaker bound to the test engine. Used by source-stage and other
    integration tests that need a real factory (not a bare session lambda)."""
    return async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture
async def session(test_engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    async with test_engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT schemaname || '.' || tablename FROM pg_tables "
                "WHERE schemaname IN ('app', 'catalog', 'news', 'ingest')"
            )
        )
        tables = [r[0] for r in result]
        if tables:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
