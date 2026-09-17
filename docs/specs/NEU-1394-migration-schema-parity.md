# NEU-1394 — Tests build the schema with create_all, not Alembic — migration-only objects are untested

**Project:** bl: Maintenance · **Milestone:** none · **Repo:** upcoming-movies-backend
**Related:** NEU-1346 (discovery context; its migration `65ba376f1b57` is the current head)

## Problem

`tests/conftest.py` builds the test schema with `Base.metadata.create_all`, so every test
exercises the model and none exercises a migration. CI (`.github/workflows/test.yml`) does run
`alembic upgrade head` against a second database before pytest, so a migration that fails to
*apply* already fails CI — but nothing checks that it builds the *same* schema the model
declares. Alembic autogenerate never emits `CheckConstraint`, so every `ck_*` is hand-written
and a forgotten one ships green. `alembic check` does not close the gap either (it ignores
check constraints).

Measured on 2026-09-17 by building two throwaway databases, one via `alembic upgrade head`
and one via `create_all`, and diffing `information_schema.columns`, `pg_constraint` and
`pg_indexes`:

- **Parity is almost complete today.** 390 catalog rows each; columns, defaults, all 24 check
  constraints, indexes, unique and primary keys match. The only drift is two FK names that
  migrations chose by hand while the model left them to Postgres:
  `fk_event_superseded_by_event` vs `event_superseded_by_fkey` (news.event) and
  `fk_event_summary_edited_by_user` vs `event_summary_edited_by_fkey` (news.event_summary).
- **`alembic downgrade base` is already broken.** Revision `26f140c4f334` ("full tmdb capture
  and normalized catalog tables", third migration ever) drops an unnamed FK on `catalog.film`
  and raises `CompileError`. Only the 35 head-most revisions downgrade cleanly.
- **Cost:** upgrade head ≈ 2.7 s, create_all ≈ 2.2 s, one-step downgrade + re-upgrade ≈ 1 s.
  Both the CI Postgres user (`root`) and the local one (`dev`) are superusers, so a test can
  create and drop its own database.

Decided (2026-09-17):

1. **A parity test against a scratch Alembic-built database.** `tests/conftest.py` keeps
   building the test DB with `create_all` — the model-first workflow in `CLAUDE.md` /
   `AGENTS.md` ("add the model column first, tests pick it up, then generate the migration")
   depends on it, and the ticket only asks for an ADR if that changes. It does not, so no ADR.
2. **Downgrade coverage is one step**, `head → -1 → head`, re-asserting parity afterwards. That
   is the round-trip NEU-1346 had to do by hand and what every future migration needs. The
   broken `26f140c4f334` downgrade is frozen history and stays as it is.
3. **The two FK names are fixed in the model**, by passing `name=` to the two `ForeignKey()`
   declarations. Prod is untouched. **No `naming_convention`** on `Base.metadata`: prod already
   carries Postgres default names (`<table>_<col>_fkey`, `<table>_pkey`, …) for every other
   constraint, so a convention would either have to mimic those defaults (no gain) or force a
   ~30-object rename migration. The parity test is what enforces name agreement from here on.

Rejected: building the test DB with Alembic (option 2 in the ticket — breaks model-first, needs
an ADR, and only catches a missing constraint if some test happens to exercise it);
introspection assertions against the `create_all` DB alone (option 1 — verifies the model, not
the migration); full `head → base → head` (would require rewriting `26f140c4f334` and possibly
other old downgrades for coverage of history nobody runs).

## What to build

### 1. Name the two FKs in the model — `src/upmovies/news/models.py`

- `Event.superseded_by`: `ForeignKey("news.event.id", ondelete="SET NULL",
  name="fk_event_superseded_by_event")`.
- `EventSummary.edited_by`: `ForeignKey("app.user.id", ondelete="SET NULL",
  name="fk_event_summary_edited_by_user")`.
- No migration: these are the names prod already has. `task makemigration` afterwards must
  produce an empty revision (autogenerate does not compare FK names, but confirm nothing else
  moved and discard the file).

### 2. Scratch migrated database — session fixture

New module `tests/integration/test_migrations.py` (fixture and tests together; nothing else
needs the fixture, so it does not go in `tests/fixtures/`).

- `migrated_db_url` (session-scoped): derive the scratch database name from
  `TEST_DATABASE_URL` via `sqlalchemy.engine.make_url` — `<test db name>_migrations`
  (`app_test_migrations` locally, `upmovies_test_migrations` in CI). Connect to the same server
  with `execution_options(isolation_level="AUTOCOMMIT")`, `DROP DATABASE IF EXISTS` then
  `CREATE DATABASE`, run `alembic upgrade head` (see below), yield the scratch URL, and drop the
  database on teardown. Schemas and extensions are *not* pre-created: `migrations/env.py`
  `_ensure_schemas` is part of what is under test (prod runs against a bare database).
- **Alembic runs as a subprocess**, `[sys.executable, "-m", "alembic", "upgrade", "head"]`
  with `DATABASE_URL=<scratch url>` in the child env and `cwd` = the repo root (where
  `alembic.ini` lives; `pyproject.toml` already sets `pythonpath = ["."]`, and
  `tests/unit/test_model_registration.py` is the precedent for subprocess tests). In-process
  `alembic.command` is not an option: `env.py` calls `asyncio.run()` (fails under the running
  pytest-asyncio session loop) and reads the URL from the lru-cached `get_settings()`, which
  conftest has already pointed at the test DB. A non-zero exit fails the fixture with the
  child's stderr in the message.
- A helper `_alembic(url, *args)` wraps the subprocess so the downgrade test reuses it.

### 3. Schema snapshot + parity test

- `_snapshot(engine) -> set[tuple]` collects, for schemas `app, catalog, news, ingest` and
  excluding `alembic_version`:
  - columns from `information_schema.columns`: `(schema.table, column_name, data_type,
    udt_name, is_nullable, column_default, character_maximum_length, numeric_precision,
    numeric_scale)` — **not** `ordinal_position` (migrations append columns; the model
    declares them in source order, and column order is not a schema difference we care about);
  - constraints from `pg_constraint` joined to `pg_class`/`pg_namespace`:
    `(schema.table, conname, contype, pg_get_constraintdef(oid))` — this is where check
    constraints, FKs (with `ON DELETE`), unique and primary keys all live, already normalised
    by Postgres so expressions compare exactly;
  - indexes from `pg_indexes`: `(schema.table, indexname, indexdef)`.
- `test_migrations_build_the_model_schema(test_engine, migrated_db_url)`: snapshot the
  `create_all`-built test DB (the existing session `test_engine`) and the scratch DB; assert
  equal. On failure print two labelled sets — *declared by the model but missing from the
  migrations* and *created by the migrations but not declared by the model* — so a missing
  `ck_*` reads as one line, not a 390-row diff.
- Open a throwaway `create_async_engine(migrated_db_url)` for the scratch snapshot and dispose
  it inside the test; do not leave a pooled connection open or the fixture's `DROP DATABASE`
  fails.

### 4. One-step downgrade test

- `test_head_migration_round_trips(test_engine, migrated_db_url)`: `_alembic(url, "downgrade",
  "-1")`, then `_alembic(url, "upgrade", "head")`, then assert the snapshot still equals the
  `create_all` snapshot. This proves the head revision's `downgrade()` runs and undoes exactly
  what `upgrade()` did (a downgrade that forgets an object leaves the re-upgrade to fail or the
  parity to break).
- Order matters only in that both tests must see a head-state DB; each ends at head, so pytest
  ordering is irrelevant.

### 5. Docs

- `CLAUDE.md` "Postgres schemas" bullet and `AGENTS.md` line "Tests use `create_all` from
  models; prod uses Alembic migrations": append one sentence — the suite proves the two agree
  (`tests/integration/test_migrations.py` builds a scratch DB with `alembic upgrade head` and
  diffs it against the `create_all` schema, and round-trips the head revision). Also the
  `AGENTS.md` **Migrations** gotcha: hand-named constraints in a migration must carry the same
  `name=` in the model, because the parity test compares names.
- No ADR (test DB is still `create_all`-built). No `CONTEXT.md` change (no domain term).

## Acceptance criteria

- [ ] With the two `name=` additions, `tests/integration/test_migrations.py` passes: the
      Alembic-built scratch DB and the `create_all` test DB have identical columns, constraints
      (including all 24 `ck_*`) and indexes, by name and definition.
- [ ] Proven negative, recorded in the PR description: temporarily deleting the
      `op.create_check_constraint("ck_event_status", …)` line from `65ba376f1b57` makes the
      parity test fail naming `ck_event_status`; the line is restored before merge.
- [ ] `alembic downgrade -1` + `upgrade head` on the scratch DB succeeds and parity holds.
- [ ] The scratch database exists only for the session: it is created by the fixture and gone
      after `task test` (`\l` in psql shows no `*_migrations` DB).
- [ ] No new migration file; `task makemigration` after the model change autogenerates nothing
      of substance.
- [ ] Suite wall-clock grows by no more than ~5 s.
- [ ] `CLAUDE.md` / `AGENTS.md` updated as in §5.
- [ ] `task format`, then `task test && task lint && task typecheck` green.

## Out of scope

- Switching `tests/conftest.py` to build the test DB with Alembic.
- A `naming_convention` on `Base.metadata` or any renaming of existing prod constraints.
- Repairing `26f140c4f334`'s downgrade or any full `downgrade base` coverage.
- A separate CI job or Taskfile task for migrations; the tests run inside `task test` / CI's
  `pytest` step like everything else. (CI's standalone `alembic upgrade head` step stays; it is
  what migrates the `upmovies` DB the API boots against.)
- Concurrency: two pytest runs on the same server would also fight over the scratch DB, the
  same limitation the shared `app_test` DB already has.
