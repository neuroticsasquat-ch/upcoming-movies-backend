# Upcoming Movies Backend — Agent Guide

## How to work in this repo

**Everything runs inside the Docker container via `task`.** Never run `pytest`, `ruff`, `pyright`, `alembic`, or `python` on the host. Source is bind-mounted; dep changes (`pyproject.toml`) need `task build`.

## Commands

| What | Command |
|------|---------|
| Build image | `task build` |
| Start (with --reload) | `task up` |
| Stop | `task down` |
| Shell in container | `task shell` |
| Full test suite | `task test` |
| Unit tests only | `task test:unit` |
| Integration tests | `task test:integration` |
| Lint | `task lint` |
| Format | `task format` |
| Typecheck | `task typecheck` |
| Coverage | `task coverage` |
| Run migrations | `task migrate` |
| New migration | `task makemigration -- "message"` |
| Prod DB refresh (local) | `task db:refresh` |

Before claiming work done: `task test && task lint && task typecheck` must all pass. Run `task format` first (ruff also reformats).

## Architecture

Single-container FastAPI service. Python 3.13, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2, httpx. Ruff + pyright + pytest. `uv` for package management.

### Layout (`src/upmovies/`)

- `app/` — auth/accounts (models, repos, services, routers)
- `catalog/` — Film/TMDB spine (models, TMDB client, upsert, seed grades)
- `news/` — Story ingestion, feeds (static `FEED_SOURCES`), fetcher
- `ingest/` — run tracking (`IngestRun`), TMDB ingest, sweep
- `public/` — API surfaces: feed (flat + grouped), film detail, calendar, sitemap
- `routers/` — FastAPI routers (public, admin, auth, etc.)
- `llm/` — LLM gateway, adapters (Anthropic, DeepInfra, DeepSeek), pricing
- `link/` — Story→film linking, retrieval, clustering, source gate
- `synthesize/` — Event summarization

DB split into Postgres schemas: `app`, `catalog`, `news`, `ingest`. Tests use `create_all` from models; prod uses Alembic migrations.

### Key patterns

- **Layering:** routers → services → repos → models. Callers own the transaction.
- **Admin auth:** `require_admin` (bearer token, machine-facing) vs `require_current_admin` (session cookie, human-facing).
- **Ingestion:** pipelines take `(session_factory, run_id, …)`, commit per item. Background tasks `asyncio.create_task` with their own session; always finalize as `failed` on crash.
- **LLM gateway:** resolves provider per stage (not per model). Never falls back. Validated at startup — misconfiguration kills the container.
- **Candidate retrieval:** lexical-only (no model call). Squash-fold + tokenization matching. `T=0.5`, `K=47`.

### Terminology (context-sensitive, enforced in code)

- **provenance:** `"story"` or `"catalog"` — where an event was *born*, never mutated when a story attaches later.
- **news_backed:** `EXISTS(event_story)` — whether *any* of a film-day's visible events has a linked story. Deliberately NOT `provenance`.
- **Catalog-sourced event:** created from TMDB field change with no story. Has deterministic summary (`model="deterministic"`) and empty `sources`.
- **Total stage failure:** stage produced nothing at all — the pipeline aborts. Distinguished from partial failure (survivors committed, run succeeds).

## Testing

- Pytest-asyncio in `auto` mode (no `@pytest.mark.asyncio` needed). Session-scoped fixtures.
- HTTP mocking via **respx** — never hit the live network.
- Integration tests use `session` fixture against test DB. Re-read after Core-level upserts with `execution_options={"populate_existing": True}`.
- Running a single integration test file in isolation errors (pytest-asyncio quirk). Run the whole suite or scope to directory.
- Stale container → async-fixture errors: `docker compose restart upmovies-backend`.

## Gotchas

- **`db:refresh` silently reverts migrations.** Restores catalog/news/ingest from prod but leaves `app` alone. Alembic version lives in `app`, so `alembic current` still reads head while tables are gone. Re-apply with `alembic stamp <prod's rev> && task migrate`.
- **Coolify shadows compose fallbacks:** a `${NAME:-default}` in compose is a seed, not a runtime default. After first deploy, Coolify stores the value and edits to the fallback are silent no-ops in prod. Change the value in the Coolify UI and restart.
- **Deploy checklist for tuned constants** (T, K, dormancy, etc.): change code default → change `docker-compose.prod.yml` → edit Coolify UI → verify with `printenv` on the running container.
- **Long-running container holds the env it was created with.** After any env change: `docker compose up -d --force-recreate upmovies-backend` and `printenv` to confirm.
- **Migrations:** add model column first (tests get it via `create_all`), then `task makemigration -- "msg"`, review, `task migrate`.


## Conventions

- Type hints: `X | None`, `X | Y` (no `Optional`/`Union`). No `from __future__ import annotations`.
- Ruff (line length 100, rules E,F,W,I,B,UP). Use `import x as x` re-export in `__init__.py` to avoid F401.
- Commits: Conventional Commits with trailing Linear ID: `feat: add X (NEU-123)`. PR titles same format.
- Branch: per ticket using Linear's generated branch name.
