# upcoming-movies-backend

FastAPI service backing the Upcoming Movies Tracker. Python 3.13, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2 / pydantic-settings, httpx, feedparser, argon2. Tooling: ruff, pyright, pytest. Packaged as a single container — **no local Python**.

## Golden rule: everything runs in the container via `task`

Do **not** run `pytest`, `ruff`, `pyright`, `alembic`, or `python` on the host. Use the `task` targets (they `docker compose exec` into the `upmovies-backend` container). Source is bind-mounted, so edits are picked up live; **dependency changes (`pyproject.toml`) require `task build`** to reinstall into the image.

| Task | Runs |
|---|---|
| `task up` / `task down` / `task build` | container lifecycle (`build` reinstalls deps) |
| `task test` | full pytest suite (`task test -- tests/unit/...` to scope) |
| `task lint` / `task format` | `ruff check src tests` / `ruff format src tests` |
| `task typecheck` | `pyright src tests` |
| `task migrate` | `alembic upgrade head` |
| `task makemigration -- "msg"` | autogenerate a migration |
| `task shell` / `task logs` | bash in container / stream logs |

Before claiming work done, `task test`, `task lint`, `task typecheck` must all be green (ruff also reformats — run `task format`).

## Layout (`src/upmovies/`)

- `app/` — auth/accounts: `models.py`, `dto.py`, `errors.py`, `repos/` (DB I/O), `services/` (business logic). Routers in `routers/` (`auth`, `me`, `invites_admin`).
- `catalog/` — `Film` (the canonical TMDB spine; UUID pk, unique `tmdb_id`).
- `news/` — `Story` (unique `url`); `feeds.py` (static `FEED_SOURCES`), `fetcher.py` (RSS/Atom → `news.story`).
- `ingest/` — generic ingestion: `models.py` (`IngestRun`), `runs.py` (run-tracking helpers), `dto.py` (`RunOut`), and `tmdb/` (`client.py`, `schemas.py`, `service.py`, `upsert.py`).
- `routers/ingest_admin.py` (triggers) + `routers/admin_runs.py` (run reads); `deps.py`, `config.py`, `db.py`, `main.py`.

DB is split into Postgres **schemas**: `app`, `catalog`, `news`, `ingest`. Tests build them from the models via `create_all` (see `tests/conftest.py`); prod uses Alembic.

## Architecture & conventions

- **Layering:** routers → services → repos → models. Repos are pure DB I/O; **callers own the transaction** (commit/rollback). Services own commits.
- **Two admin auth modes (keep them separate):**
  - `require_admin` — `ADMIN_TOKEN` bearer, machine-facing (`/admin/ingest/*` triggers+poll, `/admin/invites`). Used by the cron.
  - `require_current_admin` — session cookie + `user.is_admin`, human-facing (`/admin/runs`). Admin promotion is manual (no self-serve): `UPDATE app."user" SET is_admin = true WHERE email = '…'`.
- **Ingestion pipelines** both take `(session_factory, run_id, …)`, commit per item (one failure never rolls back others), and wire `ingest.runs` (`create_run`/`record_progress`/`finalize_run`). TMDB aborts after N consecutive failures; feeds isolate per-feed (one bad feed never fails the run). The TMDB client returns typed Pydantic DTOs and gates discover paging on a popularity floor.
- **Background tasks:** trigger endpoints `asyncio.create_task(_background_*())`; wrappers own their own session and **always finalize the run** (→ `failed` on crash). Lifespan startup cancels stale `running` runs.
- **Migrations:** add the model column first (tests get it via `create_all`), then `task makemigration -- "msg"`, review the generated file, `task migrate`.
- **Config:** `pydantic-settings`, env-aliased. Required env: `DATABASE_URL`, `ADMIN_TOKEN`, `TMDB_API_KEY`. A repo-root `.env` (gitignored) supplies `${VAR}` interpolation for `docker-compose.yml`; the running app reads env from compose. After changing required env, recreate the container (`task up`).

## Coding style

- Modern type hints: `X | None`, `X | Y` (not `Optional`/`Union`). **No** `from __future__ import annotations`.
- ruff (line length 100; rules E,F,W,I,B,UP). Use the `import x as x` re-export pattern in `__init__.py` to avoid F401.
- Tests: **TDD** (write the failing test first). pytest-asyncio is in `auto` mode (no `@pytest.mark.asyncio` needed). Mock HTTP with **respx**; never hit the live network. Integration tests use the `session` fixture against the test DB; re-read rows with `execution_options={"populate_existing": True}` after Core-level upserts.

## Gotchas

- **Running a single integration test file in isolation errors** with a pytest-asyncio session-loop warning — this is a known quirk, not your bug. Run the whole suite, or scope to a directory.
- If the long-running container starts throwing async-fixture errors across the whole suite, it's stale state: `docker compose restart upmovies-backend`.
- The TMDB client uses v3 `api_key` query auth. `TMDB_API_KEY` must be set or the app won't boot.

## Commits / PRs

Conventional commits with the Linear ID as a trailing parenthetical: `feat: add X (NEU-123)`. Keep the `🤖 Generated with Claude Code` footer; **no** `Co-Authored-By`. The GitHub↔Linear connector moves ticket status automatically — don't touch it. Branch per ticket (Linear gives the branch name).
