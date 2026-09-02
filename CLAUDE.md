# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Companion docs — read before working

- **`AGENTS.md`** — operating rules, production/deploy flow, gotchas. Authoritative on process.
- **`CONTEXT.md`** — the domain glossary. Terms there are *enforced in code* (e.g. `provenance` vs
  `news_backed`, "total stage failure" vs partial failure). When naming a concept in code, tests, or
  a PR, use the glossary's term and avoid the listed synonyms.
- **`docs/adr/`** — 16 ADRs covering retrieval, clustering, LLM routing, sweep, and feed grouping.
  Read the ones touching your area; flag explicitly if your change contradicts one.
- **`docs/specs/NEU-*.md`** — per-ticket specs (`specs_dir: docs`). Implementation work is spec-driven.

## Everything runs in the container

Never run `pytest`, `ruff`, `pyright`, `alembic`, or `python` on the host — always via `task`, which
`docker compose exec`s into the backend service. Source is bind-mounted, so uvicorn `--reload` picks
up edits; changes to `pyproject.toml` require `task build`.

| What | Command |
|---|---|
| Build / start / stop | `task build` / `task up` / `task down` |
| Shell in container | `task shell` |
| Logs | `task logs` |
| Full suite | `task test` |
| Unit / integration only | `task test:unit` / `task test:integration` |
| Single test | `task test -- tests/unit/path/test_x.py::test_name` (`--` forwards to pytest) |
| Lint / format / typecheck | `task lint` / `task format` / `task typecheck` |
| Coverage (HTML in `./htmlcov`) | `task coverage` |
| Migrate / new migration | `task migrate` / `task makemigration -- "message"` |
| Create local DBs | `task db:init` |
| Refresh local content from prod | `task db:refresh` |

**Before claiming work done:** `task format`, then `task test && task lint && task typecheck` must all
pass. CI (`.github/workflows/test.yml`) additionally runs `ruff format --check`, so unformatted code
fails the PR.

## Architecture

Single FastAPI container (Python 3.13, SQLAlchemy 2 async + asyncpg, Alembic, Pydantic v2, httpx,
`uv`). Two entrypoints share the same code:

- **`upmovies.main:app`** — the HTTP service. `create_app()` mounts routers; `lifespan` calls
  `validate_stage_configuration(settings)` (a stage routed at an unpriced/uncredentialed
  `(provider, model)` kills the container at boot, not mid-publish) and cancels runs orphaned by a crash.
- **`python -m upmovies.pipeline_run {daily|hourly|sweep}`** — the Coolify scheduled tasks, a
  *separate process* that re-runs the same startup validation. `daily` = tmdb → feeds(per-film) →
  link → synthesize, sequential and fail-fast; `hourly` = light feeds pass; `sweep` runs on its own
  slot ~2h ahead of daily and is deliberately **not** in the daily chain (ADR-0013). Each pings a
  healthchecks.io deadman (`/start`, base, `/fail`).

### Layout (`src/upmovies/`)

`app/` auth & accounts (models/repos/services) · `catalog/` Film/TMDB spine · `news/` stories, feeds,
events · `ingest/` run tracking + TMDB ingest + `sweep/` · `link/` story→film linking, `retrieval/`,
clustering, source gate · `synthesize/` event summarization · `llm/` gateway + provider adapters +
pricing · `public/` read models for feed/film/calendar/sitemap · `routers/` FastAPI routers.

### Cross-cutting patterns

- **Layering:** routers → services → repos → models. Callers own the transaction.
- **Model registration:** `upmovies/models.py` aggregate-imports every model module and `db.py`
  imports it last, so `Base.metadata` is complete (and cross-schema FKs resolve) even for standalone
  scripts. **Register new model modules there.**
- **Postgres schemas:** `app`, `catalog`, `news`, `ingest`. Tests build them with `create_all`; prod
  uses Alembic. Add the model column first (tests pick it up immediately), then generate and review
  the migration.
- **Pipelines** take `(session_factory, run_id, …)`, commit per item, and always finalize their run —
  `failed` on crash — because `routers/ingest_admin.py` reuses the same runners.
- **LLM gateway** resolves a provider per *stage* (link, cluster, summarize, source_judge), never per
  model, and **never falls back** — answering one stage from another provider would misattribute cost
  and latency.
- **Admin auth is two things:** `require_admin` (bearer `ADMIN_TOKEN`, machine-facing) vs
  `require_current_admin` (session cookie + `is_admin`, human-facing UI). `require_csrf` guards
  cookie-authed mutations.

## Testing

- pytest-asyncio in `auto` mode with session-scoped loops — no `@pytest.mark.asyncio`.
- `tests/conftest.py` points `DATABASE_URL` at `TEST_DATABASE_URL`, drops/recreates the four schemas
  per session, and truncates between tests via the `session` fixture.
- HTTP is mocked with **respx**; never hit the live network.
- After Core-level upserts, re-read with `execution_options={"populate_existing": True}`.
- Running a single *integration* test file in isolation errors (pytest-asyncio quirk) — run the whole
  suite or scope to a directory.
- Async-fixture errors usually mean a stale container: restart the backend service.

## Conventions

- Type hints use `X | None` / `X | Y` — no `Optional`/`Union`, no `from __future__ import annotations`.
- Ruff: line length 100, rules `E,F,W,I,B,UP`. Use `import x as x` re-exports in `__init__.py`.
- Commits and PR titles: Conventional Commits with a trailing Linear ID — `feat: add X (NEU-123)`.
  Branch per ticket using Linear's generated name.
- The frontend is a sibling repo at `../frontend`; read its `AGENTS.md` before
  touching it.
