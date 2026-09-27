"""The per-test database reset behind the `session` fixture (NEU-1506).

The suite used to end every DB-backed test with one `TRUNCATE <every table> RESTART IDENTITY
CASCADE`. TRUNCATE does not empty a table in place: it gives the table and each of its indexes a
new relation file, ~150 files per teardown at 47 tables, and every one of them is fsynced at
the next checkpoint. That was ~45 ms per test, and a checkpoint that stalled whichever test
triggered it (a `DROP DATABASE` in `test_migrations.py`) for ~45 s.

The reset here writes in place instead. It finds the tables that hold rows, DELETEs from just
those, and puts every sequence back at its start value. The probe reads the data, not which
session wrote it, so rows committed through a test's own `session_factory` or a runner's
sessions are found like any other."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class DatabaseInventory:
    """What `reset_database` resets, read once per session after `create_all`: the tables
    and sequences are fixed from then on, since nothing in the suite creates tables at run
    time."""

    probe: str
    """One `UNION ALL` of `EXISTS` probes, returning the names of the tables holding rows."""
    restart: str
    """One `SELECT setval(...)` over every sequence, back to its declared start value."""


def _qualified(schema: str, name: str) -> str:
    # Quoted even where Postgres would not need it (`app.user` works unquoted only because it
    # is schema-qualified).
    return ".".join('"' + part.replace('"', '""') + '"' for part in (schema, name))


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def read_inventory(conn: AsyncConnection, schemas: Sequence[str]) -> DatabaseInventory:
    """Read the tables and sequences in `schemas` and prebuild the two statements
    `reset_database` runs, so no teardown queries the catalog."""
    params = {"schemas": list(schemas)}
    tables = [
        _qualified(schema, name)
        for schema, name in await conn.execute(
            text(
                "SELECT schemaname, tablename FROM pg_tables "
                "WHERE schemaname = ANY(:schemas) ORDER BY 1, 2"
            ),
            params,
        )
    ]
    sequences = [
        (_qualified(schema, name), start)
        for schema, name, start in await conn.execute(
            text(
                "SELECT schemaname, sequencename, start_value FROM pg_sequences "
                "WHERE schemaname = ANY(:schemas) ORDER BY 1, 2"
            ),
            params,
        )
    ]
    probe = " UNION ALL ".join(
        f"SELECT {_literal(t)} WHERE EXISTS (SELECT 1 FROM {t})" for t in tables
    )
    # `setval(seq, start, false)` is what `RESTART IDENTITY` did: the next `nextval` returns the
    # start value. Not `ALTER SEQUENCE ... RESTART`, which gives the sequence a new relation
    # file just as TRUNCATE does a table; `setval` writes in place.
    restart = "SELECT " + ", ".join(
        f"setval({_literal(s)}, {start}, false)" for s, start in sequences
    )
    return DatabaseInventory(probe=probe, restart=restart)


async def reset_database(conn: AsyncConnection, inventory: DatabaseInventory) -> None:
    """Empty every table in the inventory and restart every sequence, inside the caller's
    transaction (`engine.begin()`).

    Every sequence restarts, not only those of the tables that held rows: a test that flushes
    a row and rolls back advances a sequence without leaving one, and ids would then drift
    across tests where `RESTART IDENTITY` never let them.

    **Needs a superuser.** `session_replication_role = replica` is what lets the DELETEs run
    in any order (no FK trigger fires, nor any other trigger — TRUNCATE never fired row
    triggers either), and setting that parameter requires superuser. The dev role (`dev`)
    and CI's service role (`root`, `POSTGRES_USER` in `.github/workflows/test.yml`) both are;
    under any other role this fails with "permission denied to set parameter
    session_replication_role".

    Rows another connection has written but not yet committed are not seen, where TRUNCATE
    would have waited for them. No test leaves such a transaction open past its end; one that
    did would leak its rows into the next test once it committed."""
    dirty = (await conn.execute(text(inventory.probe))).scalars().all()
    if dirty:
        # LOCAL scopes the role to this transaction, so the pooled connection goes back clean.
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        # One statement per execute: asyncpg prepares each one, and refuses a script.
        for table in dirty:
            await conn.execute(text(f"DELETE FROM {table}"))
    await conn.execute(text(inventory.restart))
