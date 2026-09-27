# NEU-1506 — Test teardown truncates all 47 tables per test, and the file churn stalls checkpoints for minutes

**Project:** bl: Maintenance · **Milestone:** none · **Repo:** upcoming-movies-backend
**Related:** NEU-1393 (previous suite-speed pass; its "teardown is fine" note is now stale),
NEU-1469 (added the trigram indexes and the film trigger), NEU-1505 (discovery context),
NEU-1507 (follow-up: run Alembic in-process in `test_migrations.py`)

## Problem

The full suite takes 303 s (3,774 tests, measured 2026-09-27); it was ~83 s when NEU-1393 was
filed. The `session` fixture in `tests/conftest.py` tears every DB-backed test down (~2,086 of
them) with one `TRUNCATE <all 47 tables> RESTART IDENTITY CASCADE`. TRUNCATE does not empty a
table in place: it gives each table and each of the 105 indexes a fresh relation file, so one
teardown creates ~150 files. Two costs follow:

- **Per test:** the teardown alone has a median of 45 ms (p90 52 ms), ~94 s of the run.
- **Checkpoints:** every one of those files is fsynced at the next checkpoint. One run showed a
  forced checkpoint of 479,381 files taking 44.6 s (the `DROP DATABASE` in `m8_db_url`'s
  teardown, reported by pytest as a 44.67 s teardown on
  `test_the_m8_migration_turns_chosen_watchlist_rows_into_title_follows`) and a background
  checkpoint of 408,184 files over 171 s competing for I/O while tests ran.

Measured on a scratch database built the way conftest builds `app_test` (3 rows into 3 tables
per iteration, N=150):

| Teardown | median | p90 |
| -- | -- | -- |
| A. current: TRUNCATE all | 45.2 ms | 51.8 ms |
| B. A + `synchronous_commit = off` | 46.3 ms | 53.1 ms |
| C. DELETE from non-empty tables only | **6.3 ms** | 7.1 ms |
| D. C + `synchronous_commit = off` | 5.2 ms | 6.0 ms |

`synchronous_commit = off` does nothing for TRUNCATE (the cost is file churn, not WAL flush),
but it takes a single-row commit from 2.27 ms to 0.92 ms, and the import runners and sweep
phases commit per row. It is a separate, cheap win for the test database only.

Decided (2026-09-27):

- **Variant C replaces the TRUNCATE.** Probe which tables hold rows, DELETE from exactly those
  under `session_replication_role = replica`, and restart sequences. No new relation files, so
  the checkpoint stalls go with it.
- **Every sequence restarts on every teardown**, not only those owned by dirty tables (the
  ticket's wording). A test that flushes a row and rolls back advances a sequence without
  leaving a row; restarting only dirty tables' sequences would let ids drift across tests where
  `TRUNCATE ... RESTART IDENTITY` did not. There are 10 sequences in the four schemas, all
  plain serials (no identity columns), and a restart is an in-place write, so restarting all of
  them costs nothing measurable and keeps today's semantics exactly.
- **`synchronous_commit = off` is set per connection on the test engine**, via asyncpg's
  `server_settings` in `create_async_engine(connect_args=...)`. CI never runs `task db:init`,
  so an `ALTER DATABASE` there would not reach CI; the engine setting covers the container and
  CI alike and cannot touch the dev `app` database. **Not `fsync = off`:** server-wide, would
  put `app` at risk, and `docker-compose.yml` is Coder-rendered and not editable here anyway.

## What to build

### 1. Session-scoped inventory — `tests/conftest.py`

Once per session, after `Base.metadata.create_all` has run, read and keep:

- the table list: every table in `app`, `catalog`, `news`, `ingest` from `pg_tables`, as
  quoted `"schema"."table"` identifiers (`app.user` works today unquoted only because it is
  schema-qualified; quote anyway);
- the sequence list: every sequence in those schemas from `pg_sequences`, quoted the same way;
- the emptiness probe, prebuilt once from the table list: one statement of the shape
  `SELECT '<qualified>' WHERE EXISTS (SELECT 1 FROM <qualified>) UNION ALL ...`, returning the
  names of the tables that currently hold at least one row.

The lists are only inputs: what is kept is the probe and the one-statement sequence restart
built from them (§2), since nothing else reads the lists. Keep this on a session-scoped fixture (or on the `test_engine` fixture's yield value) rather
than re-querying the catalog per test.

### 2. The teardown — `session` fixture in `tests/conftest.py`

After the test's session is rolled back and closed, in one `engine.begin()` transaction:

1. Run the probe. If it names any tables:
   - `SET LOCAL session_replication_role = replica` so FK order does not matter and no trigger
     fires (the NEU-1469 `film_field_change_trg` on `catalog.film` included; TRUNCATE never
     fired row triggers either, so this is not a behaviour change);
   - `DELETE FROM <table>` for each named table.
2. Always: `setval(<seq>, <start_value>, false)` for every sequence in the inventory, in one
   `SELECT`. That returns each sequence to its declared start value, which is what
   `RESTART IDENTITY` did. **Not `ALTER SEQUENCE <seq> RESTART`**: on Postgres 17 it gives the
   sequence a new relation file (checked with `pg_relation_filenode` during implementation),
   the very churn this ticket removes; `setval` writes in place.

Notes for the implementer:

- asyncpg refuses multi-statement strings through SQLAlchemy `text()` (it prepares
  statements). One statement per `execute`, or drive the script through the raw asyncpg
  connection. The ticket's 6.3 ms was measured with separate statements, so per-statement
  round trips are within budget.
- `SET LOCAL` needs an open transaction; `engine.begin()` provides one, and `LOCAL` scopes the
  role change to it so the pooled connection is clean afterwards.
- The probe reads data, not sessions, so rows written through a test's own `session_factory`
  session or the runners' own sessions are found and removed like any other.

### 3. Superuser requirement, stated — `session` fixture docstring

`session_replication_role` needs superuser. The dev role (`dev`) and CI's service role
(`root`, from `POSTGRES_USER` in `.github/workflows/test.yml`) both are. Say so in the
fixture's docstring, naming the parameter, so a future non-superuser CI role fails with a
readable pointer rather than a bare "permission denied to set parameter".

### 4. `synchronous_commit = off` — `test_engine` fixture

```python
engine = create_async_engine(
    url,
    pool_pre_ping=True,
    connect_args={"server_settings": {"synchronous_commit": "off"}},
)
```

Comment it in the conftest style (see the `RATE_LIMIT_ENABLED` and hasher comments): the
setting is test-database-only, is *not* `fsync`, and exists for the row-by-row committers
(import runners, sweep phases), not for the teardown.

### 5. A test for the teardown helper

Factor the per-test reset into a plain coroutine (e.g. `reset_database(conn, inventory)`) and
give it one direct test under `tests/integration/`: insert rows into two or three tables across
schemas through a raw connection, call the helper, assert every table is empty, and assert the
next `nextval` on an advanced sequence is its start value. This is the only deterministic way
to prove the restart semantics; a two-test ordering pair would depend on collection order.

## Acceptance criteria

- Full-suite time is measurably down from 303 s; expect roughly 80 s back from the teardown
  plus most of the checkpoint stall. Record the before/after in the PR description.
- `task test -- --durations=20` shows no multi-second `teardown` on any `test_migrations.py`
  test (the `alembic upgrade` *setup* cost remains; that is NEU-1507).
- Identity semantics are unchanged: after any teardown every sequence in the four schemas is
  at its start value, whether or not its table held rows.
- Rows written through a test's own session factory or engine against `app_test` are gone
  after teardown (the probe reads data, not sessions).
- The `session` fixture docstring names the superuser requirement and the parameter behind it.
- `synchronous_commit` is off on `test_engine` connections and nowhere else; `SHOW
  synchronous_commit` from a test connection returns `off`, and the dev `app` database and
  `docker-compose.yml` are untouched.
- `task test && task lint && task typecheck` green in the container and in CI.

## Watch for

- Tests that rely on ids starting at 1. Covered by restarting every sequence, every time.
- Tests that write through their own `session_factory` or engine. The probe catches them; the
  only engines in tests that do not point at `app_test` are the scratch databases in
  `test_migrations.py`, which drop themselves.
- The inventory is read once. A test that creates a table at runtime would be missed; none
  does today, and `create_all` is the only schema writer in the suite.

## Out of scope / deferred

- `test_migrations.py`'s ~40 s of `alembic` subprocess startups: **NEU-1507** (in-process
  Alembic; the migrations themselves cost ~0.5 s for the whole chain).
- `fsync = off`, any `postgresql.conf` or `docker-compose.yml` change.
- Changing the CI role or workflow; the existing `root` superuser is sufficient.
