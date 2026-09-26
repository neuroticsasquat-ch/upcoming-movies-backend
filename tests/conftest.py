import os

os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ.pop("COOKIE_DOMAIN", None)
# The rate limiter is off for the suite as a whole (NEU-1344). Every route test shares one
# client address, so with it on the fifth signup in the suite would 429 whichever test happened
# to run fifth — the limiter would be measuring pytest, not the route. `RATE_LIMIT_ENABLED` is
# the documented switch for exactly this. The tests that exercise the limiter turn it back on
# by overriding `get_settings` with their own `Settings`, in
# `tests/integration/routers/test_rate_limit.py`.
os.environ["RATE_LIMIT_ENABLED"] = "false"
# The *outbound* TMDB window is shared process-wide since NEU-1399, and the suite is one
# process — so every test that reaches `TMDBClient.from_settings` (the route and pipeline
# tests that drive production code: `test_pipeline_run.py`, `routers/test_ingest_admin.py`,
# `routers/test_imports.py`) would otherwise spend one cumulative 40-per-10s budget between
# them and start waiting on each other's requests. Same reasoning as the line above: make it
# inert for the tests that are not about it. Tests that construct `TMDBClient` directly are
# unaffected either way — the raw constructor still gets a window of its own — and
# `tests/unit/ingest/tmdb/test_client.py` builds its own `Settings` at a tiny window to
# exercise the sharing. The window length is left alone; only the capacity moves.
os.environ["TMDB_RATE_LIMIT_REQUESTS"] = "100000"

pytest_plugins = [
    "tests.fixtures.users",
    "tests.fixtures.public",
    "tests.fixtures.mail",
    "tests.fixtures.turnstile",
]

from upmovies.config import Settings  # noqa: E402

# The workspace compose bind-mounts the whole repo at /app, so `.env` -- the local template
# carrying every optional setting -- now sits exactly where pydantic-settings looks for it.
# A test that clears a variable to assert a default, or to assert the required-field error,
# would read the value straight back out of that file. The process environment is the only
# source a test should see, so Settings reads no env file here.
Settings.model_config["env_file"] = None

from collections.abc import AsyncIterator, Iterator  # noqa: E402

import pytest  # noqa: E402
from argon2 import PasswordHasher  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import upmovies.models  # noqa: F401, E402  -- register every model with Base.metadata
from upmovies.app import passwords  # noqa: E402
from upmovies.db import Base  # noqa: E402

_SCHEMAS = ("app", "catalog", "news", "ingest")


# Password hashing runs at argon2's minimum cost for the suite as a whole (NEU-1393). The
# production `PasswordHasher()` defaults (t=3, m=64 MiB, p=4) cost ~70 ms per hash, and every
# user the `make_user` fixture creates pays one -- ~10-20 s of the run, spread too thin to show
# in `--durations`, deriving hashes whose strength no test asserts. argon2 `verify` reads the
# parameters back out of the hash string, so a cheap hash also verifies cheaply and the verify
# side needs nothing. `hash_password` / `verify_password` read the module global on every call,
# which is what makes swapping it here reach `account_service`, `reset_service`,
# `email_change_service` and the fixtures alike; `src/` is untouched. The fixture yields the
# hasher `passwords.py` really built so the one file that tests the hasher itself,
# `tests/unit/app/test_passwords.py`, can put that exact object back for its own tests --
# asserting on production's parameters means nothing if the test constructs its own hasher.
@pytest.fixture(scope="session", autouse=True)
def cheap_password_hasher() -> Iterator[PasswordHasher]:
    production = passwords._hasher
    passwords._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    yield production
    passwords._hasher = production


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
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
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
