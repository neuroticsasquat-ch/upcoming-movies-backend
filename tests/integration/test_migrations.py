"""Migration ↔ model schema parity (NEU-1394).

The suite builds its database with ``Base.metadata.create_all`` (``tests/conftest.py``), so no
ordinary test ever runs a migration. Alembic autogenerate never emits ``CheckConstraint``, so
every ``ck_*`` is hand-written, and a forgotten one would otherwise ship green. These tests
build a second, scratch database with ``alembic upgrade head`` from a bare database -- the way
prod is built -- and diff its columns, constraints and indexes against the ``create_all``
schema. A missing constraint reads as one line of the assertion message, not a 390-row diff.

The head revision is also round-tripped (``downgrade -1`` → ``upgrade head``) so its
``downgrade()`` is proven to undo exactly what ``upgrade()`` did. Older downgrades are frozen
history and not covered (``26f140c4f334`` cannot downgrade at all).
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = "'app', 'catalog', 'news', 'ingest'"

Snapshot = set[tuple[object, ...]]


def _alembic(url: str, *args: str) -> None:
    # Alembic runs as a subprocess, never in-process: `migrations/env.py` calls `asyncio.run()`,
    # which fails under pytest-asyncio's running session loop, and reads the URL from the
    # lru-cached `get_settings()`, which conftest has already pointed at the test DB.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stderr}")


@pytest.fixture(scope="session")
async def migrated_db_url() -> AsyncIterator[str]:
    """A scratch database built by ``alembic upgrade head`` from nothing -- no schemas, no
    extensions -- so ``env.py``'s bootstrap is under test too. Dropped after the session."""
    test_url = make_url(os.environ["TEST_DATABASE_URL"])
    scratch_url = test_url.set(database=f"{test_url.database}_migrations")
    scratch_db = scratch_url.database
    # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    admin = create_async_engine(test_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        url = scratch_url.render_as_string(hide_password=False)
        try:
            _alembic(url, "upgrade", "head")
            yield url
        finally:
            # Also on a failed upgrade -- the case this fixture exists to catch -- so a broken
            # migration never leaves the scratch database behind.
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
    finally:
        await admin.dispose()


async def _snapshot(engine: AsyncEngine) -> Snapshot:
    """Every column, constraint and index in the four app schemas, keyed by name and
    definition. Column order is deliberately excluded: migrations append columns, the model
    declares them in source order, and that is not a schema difference."""
    columns = text(
        "SELECT 'column', table_schema || '.' || table_name, column_name, data_type, udt_name, "
        "is_nullable, column_default, character_maximum_length, numeric_precision, numeric_scale "
        "FROM information_schema.columns "
        f"WHERE table_schema IN ({_SCHEMAS}) AND table_name <> 'alembic_version'"
    )
    # pg_constraint is where check constraints, FKs (with ON DELETE), unique and primary keys
    # all live, and pg_get_constraintdef normalises the expression so it compares exactly.
    constraints = text(
        "SELECT 'constraint', n.nspname || '.' || c.relname, con.conname, con.contype::text, "
        "pg_get_constraintdef(con.oid) "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname IN ({_SCHEMAS}) AND c.relname <> 'alembic_version'"
    )
    indexes = text(
        "SELECT 'index', schemaname || '.' || tablename, indexname, indexdef "
        f"FROM pg_indexes WHERE schemaname IN ({_SCHEMAS}) AND tablename <> 'alembic_version'"
    )
    rows: Snapshot = set()
    async with engine.connect() as conn:
        for query in (columns, constraints, indexes):
            rows.update(tuple(row) for row in await conn.execute(query))
    return rows


async def _migrated_snapshot(url: str) -> Snapshot:
    # A throwaway engine, disposed here: a pooled connection left open would make the
    # fixture's DROP DATABASE fail.
    engine = create_async_engine(url)
    try:
        return await _snapshot(engine)
    finally:
        await engine.dispose()


def _assert_parity(model: Snapshot, migrated: Snapshot) -> None:
    missing = sorted(map(repr, model - migrated))
    extra = sorted(map(repr, migrated - model))
    assert not missing and not extra, (
        "\nDeclared by the model but missing from the migrations:\n  "
        + "\n  ".join(missing or ["(none)"])
        + "\nCreated by the migrations but not declared by the model:\n  "
        + "\n  ".join(extra or ["(none)"])
    )


async def test_migrations_build_the_model_schema(test_engine: AsyncEngine, migrated_db_url: str):
    model = await _snapshot(test_engine)
    migrated = await _migrated_snapshot(migrated_db_url)
    _assert_parity(model, migrated)


async def test_head_migration_round_trips(test_engine: AsyncEngine, migrated_db_url: str):
    # The round-trip NEU-1346 had to do by hand: the head revision's downgrade must run, and
    # re-upgrading must land back on the model's schema. Ends at head, so test order is moot.
    _alembic(migrated_db_url, "downgrade", "-1")
    _alembic(migrated_db_url, "upgrade", "head")
    model = await _snapshot(test_engine)
    migrated = await _migrated_snapshot(migrated_db_url)
    _assert_parity(model, migrated)
