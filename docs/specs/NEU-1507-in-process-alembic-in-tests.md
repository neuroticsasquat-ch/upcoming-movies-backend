# NEU-1507 — Migration tests pay ~2.4 s of Python startup per alembic subprocess; run alembic in-process

**Project:** bl: Maintenance · **Milestone:** none · **Repo:** upcoming-movies-backend
**Related:** NEU-1506 (split from; removes the checkpoint stall that inflated one of these
teardowns to 44 s), NEU-1394 (wrote the parity tests and the subprocess helper)

## Problem

`tests/integration/test_migrations.py` runs Alembic 15 times per suite through the `_alembic`
helper, each as a `python -m alembic` subprocess: one `upgrade head` for the session-scoped
`migrated_db_url`, a `downgrade -1` / `upgrade head` pair for the round-trip test, and for each
of the six function-scoped scratch fixtures (`m8_db_url`, `binary_follows_db_url`,
`dismissal_drop_db_url`, `credits_backfill_db_url`, `unsubscribe_token_db_url`,
`digest_only_db_url`) an `upgrade <revision-before-the-one-under-test>` in setup and an
`upgrade head` in the test body. Together they cost ~40 s of the suite.

The ticket as first filed blamed chain replay and proposed template databases. Measured in the
dev container on 2026-09-27, that premise was wrong:

| Invocation | wall time |
| -- | -- |
| `python -c "import upmovies.models"` | 1.95 s |
| `python -m alembic current` | 2.44 s |
| `python -m alembic upgrade head` from an empty database (all 67 revisions) | 2.94 s |
| `python -m alembic upgrade head` from head-1 (one revision) | 2.47 s |
| `CREATE DATABASE ... TEMPLATE` of a schema-only database | 0.12 s |

The migrations themselves cost ~0.5 s for the whole chain. The other ~2.4 s of every
invocation is interpreter startup plus importing the model graph, and pytest has already paid
that once. 15 × 2.4 s ≈ 36 s is the cost; templates cannot touch it.

Decided (2026-09-27): **run Alembic in-process, and drop the subprocess.** No template
databases (a ladder of cloned templates would save a further ~3 s of in-process replays for a
session-scoped fixture with ordering logic; not worth it at that size). The scratch-database
fixtures keep their shape: `CREATE DATABASE` from nothing, so `env.py`'s schema and extension
bootstrap stays under test, `DROP DATABASE` afterwards.

The two things the file's comment names as the reason for the subprocess both have small
answers:

- **`env.py` calls `asyncio.run()`**, which fails under pytest-asyncio's running session loop.
  Run the command in a worker thread via `asyncio.to_thread`; a fresh thread has no running
  loop, `asyncio.run()` builds its own, and `run_migrations_online` disposes its `NullPool`
  engine before that loop closes.
- **`env.py` takes its URL from the lru-cached `get_settings()`**, which conftest has already
  pointed at `app_test`. Let it prefer `config.attributes["url"]` when set. `Config.attributes`
  is Alembic's documented channel for values passed from application code; the CLI never sets
  it, so production `alembic upgrade head` is unchanged.

## What to build

### 1. `migrations/env.py` — one line

Replace the unconditional `config.set_main_option("sqlalchemy.url", get_settings().database_url)`
with a lookup that prefers `config.attributes.get("url")` and falls back to
`get_settings().database_url`. Comment it: the attribute is set only by
`tests/integration/test_migrations.py`, which runs Alembic in-process against scratch
databases; the CLI path is the fallback and is what prod uses.

Nothing else in `env.py` changes. `asyncio.run(run_migrations_online())` stays; the tests
arrange to call it off the event-loop thread.

*Amended at implementation (2026-09-27):* the `%` → `%%` escape lives here, on the value
`env.py` passes to `set_main_option`, not in the test helper. `env.py` re-sets
`sqlalchemy.url` from whichever URL it resolved, so an escape applied only in the helper would
be overwritten and a `%`-bearing URL would still raise here. For a URL without `%` the CLI
behaves exactly as before; one with `%` (a URL-encoded password) used to raise
`invalid interpolation syntax` and now works.

### 2. `tests/integration/test_migrations.py` — replace `_alembic`

Replace the subprocess helper with an async one of the same call shape,
`await _alembic(url, "upgrade", rev)` / `await _alembic(url, "downgrade", "-1")`, that:

- builds an `alembic.config.Config()` **without** a config file name, so `env.py`'s
  `fileConfig(...)` branch is skipped (in-process, `logging.config.fileConfig` would replace
  pytest's log handlers and, with its default `disable_existing_loggers=True`, silence the
  app's loggers for the rest of the run);
- sets `script_location` to `<repo>/migrations` (absolute, from `_REPO_ROOT`), sets
  and sets `config.attributes["url"] = url` (no `sqlalchemy.url` main option of its own:
  `env.py` sets that from the attribute before `async_engine_from_config` reads the section,
  and `set_main_option` creates the `alembic` section on a file-less `Config()`);
- dispatches to `alembic.command.upgrade` / `alembic.command.downgrade` through
  `asyncio.to_thread`, so the call site stays `await`-able and pytest-asyncio's loop is not
  the one `env.py` tries to start a loop under.

Exceptions from a failing migration now propagate directly instead of arriving as a
`RuntimeError` wrapping stderr; the `migrated_db_url` fixture's "also on a failed upgrade"
cleanup must still run (it is a `finally`, so it does).

Update the module docstring and the comment that explained why Alembic ran as a subprocess;
`AGENTS.md`'s line on the parity test does not mention the subprocess and needs no change.

Every `_alembic(...)` call site gains an `await`. The `upgrade head` in each test body stays
`head` (not the single next revision): in-process it is cheap, and the seeded rows crossing the
successor migrations is a property the tests have always had.

### 3. Nothing else

No Taskfile, CI workflow, `alembic.ini` or conftest change. `alembic.ini` still carries the
placeholder `sqlalchemy.url` the CLI overrides through `env.py`.

## Acceptance criteria

- `task test -- tests/integration --durations=20` shows no `test_migrations.py` entry above
  ~1 s in `setup` or `call`, and the seven tests together take well under 10 s (from ~40 s).
- No `subprocess` import remains in `test_migrations.py`; every Alembic run in the suite is
  in-process.
- `alembic upgrade head` from the CLI against a bare database still bootstraps the four schemas
  and three extensions and lands on the model schema: the parity test and round-trip test
  still pass, and a manual `task migrate` against the dev `app` database still works (that is
  the code path the `config.attributes` fallback must leave intact).
- Each migration under test still starts from the same revision it does today (the six
  `_BEFORE_*` constants are untouched).
- pytest's logging is not reconfigured by the migration tests: a `caplog`-using test placed
  after them in collection order still captures.
- `task test && task lint && task typecheck` green in the container and in CI.

## Watch for

- `env.py` executes at module level on every command (Alembic re-runs the script each time),
  so the thread-per-call pattern is exactly what the CLI does, once per process; nothing is
  cached across calls.
- `Config.set_main_option` interpolates `%`. The dev and CI URLs contain none; `env.py` escapes
  anyway (see §1's amendment).
- The scratch `DROP DATABASE` still needs every connection to that database closed.
  `run_migrations_online` disposes its engine; `_migrated_snapshot` disposes its own; test
  bodies that open an engine already `dispose()` in a `finally`.
- NEU-1506's checkpoint-stall fix is what makes the `DROP DATABASE` fast; measured
  before NEU-1506 lands, a drop can still read as seconds.

## Out of scope / deferred

- Template databases (`CREATE DATABASE ... TEMPLATE`): measured at 0.12 s per clone, but the
  in-process replays they would replace cost ~0.5 s each; not worth a session-scoped ladder.
- Re-measuring `tests/unit/test_model_registration.py`'s deliberate whole-graph import.
- Any change to how prod runs migrations.
