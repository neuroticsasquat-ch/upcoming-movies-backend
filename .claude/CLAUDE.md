# upcoming-movies-backend

FastAPI service backing the Upcoming Movies Tracker. Python 3.13, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2 / pydantic-settings, httpx, feedparser, argon2. Tooling: ruff, pyright, pytest. Packaged as a single container — **no local Python**.

## Linear

- `linear_initiative`: backlotter
- `linear_team`: Neuroticsasquatch
- `loop_base`: main — v0.3.0 shipped 2026-08-11 and `release/v0.3.0` is fully merged, so work
  branches from `main` again. **Repoint this when the next release branch is cut**, and back to
  `main` when it merges; a stale value here silently forks new work off a dead branch.

## Docs

- `specs_dir`: `../docs` — the umbrella `~/projects/upcoming-movies/docs`, shared with the
  frontend repo. Specs in `specs/`, plans in `plans/`.

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
| `task db:refresh` | pull prod `catalog`/`news`/`ingest` into the local DB — **local `app`/user data untouched** |
| `task db:refresh:app` / `db:refresh:all` | same, including `app` — drops local users, prompts first |

`db:refresh` needs `PROD_SSH` in `.env` (see `.env.example`); it reads prod only, and every write goes through the local Postgres container. Use it before any measurement script — the dev catalog is a fraction of prod's, so accuracy numbers taken against it don't mean much.

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
- **`task db:refresh` silently reverts migrations while Alembic still reports head.** It restores `catalog`/`news`/`ingest` from prod but leaves `app` alone — and `alembic_version` lives in `app`. So after a refresh the local DB carries prod's (older) content schemas while `alembic current` reads head, and any table or column a recent migration added to those three schemas is simply gone. Re-apply with `alembic stamp <prod's revision> && task migrate`; don't trust `alembic current` after a refresh.
- **Coolify shadows the compose fallbacks, so changing a tuned constant takes two edits.** A `${NAME:-default}` in `docker-compose.prod.yml` is a **seed, not a runtime default**: Coolify parses it the first time it meets the variable, stores a UI entry with that value, and from then on the UI entry is what reaches the container. Editing the fallback later is a **silent no-op in production**. NEU-1088 moved K from 25 to 35 in the code and in the compose file, CI passed, the deploy succeeded — and prod ran 25 for another hour. A variable that is genuinely *new* does come through at its fallback, which is the tell (and a handy tracer for whether a deploy landed). Coolify also **refuses to delete** a UI entry whose name appears in the compose file, so the fix is to edit the value there, not to remove it — and don't strip the `${...}` line to force a delete, because that is what makes the constant tunable without a redeploy in the first place.
- **Deploy checklist for any tuned constant** (T, K, the health thresholds, dormancy, batch size): change the code default → change the fallback in `docker-compose.prod.yml` (`test_prod_compose_fallbacks_match_the_code_defaults` pins the retrieval ones) → **edit the value in the Coolify UI and restart** → verify against the running container, not the compose file:
  ```bash
  ssh <PROD_SSH> 'docker exec $(docker ps --format "{{.Names}}" | grep -i upmovies-backend | head -1) printenv | grep LINK_RETRIEVAL | sort'
  ```
  Check the container's name and created-time too — Coolify replaces the container on deploy, so a reading taken mid-deploy can come from the outgoing one.
- **The compose-fallback test guards the seed, not production — and it only runs in CI.** `test_prod_compose_fallbacks_match_the_code_defaults` needs `docker-compose.prod.yml`, which is not mounted into the dev container, so it **skips** locally and runs against the checkout in CI. It was briefly mounted; a single-file bind mount tracks the inode, so every `git checkout` replaced the file and left the mount dangling, failing the suite on a path that was present on the host all along. A skip you can see beats a mount that breaks on every branch switch.
- **A long-running container holds the env it was created with.** Editing `docker-compose.yml` does not change a running container, so `printenv` inside it can disagree with the compose file for hours. `docker compose up -d --force-recreate upmovies-backend` after any env change, and `printenv` to confirm rather than reading the compose file.
- The TMDB client uses v3 `api_key` query auth. `TMDB_API_KEY` must be set or the app won't boot.

## Commits / PRs

Use `/commit-msg` for commit messages and `/pr-desc` for PR descriptions, post-processed to Conventional Commits.

- **Commits:** Conventional Commits subject with a trailing Linear ID parenthetical — `feat: add X (NEU-123)`, same format as the PR title. Drop the parenthetical when no ticket maps to the change (don't invent one). **No** `🤖 Generated with Claude Code` footer; **no** `Co-Authored-By`. Commits before 2026-08-11 keep the ID out of the subject and carry `Refs NEU-123.` in the body instead — that was the old rule; don't copy it from history.
- **PR title:** Conventional Commits + trailing Linear ID parenthetical: `feat: add X (NEU-123)`. The `🤖 Generated with Claude Code` footer is fine in the PR **body** only.
- The GitHub↔Linear connector moves ticket status automatically — don't touch it. Branch per ticket (Linear gives the branch name).

## Agent skills

### Issue tracker

Issues and PRDs live in **Linear** (team Neuroticsasquatch, initiative "Upcoming Movies Tracker", `NEU-###` tickets) via the Linear MCP; a GitHub↔Linear connector moves workflow state automatically. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles map to same-named **Linear labels** (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), applied on top of workflow state. See `docs/agents/triage-labels.md`.

### Domain docs

**Single-context**: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
