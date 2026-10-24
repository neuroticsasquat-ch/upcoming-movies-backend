# upcoming-movies-backend

FastAPI service backing the Upcoming Movies Tracker.

Stack: Python 3.13, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2, httpx, uv. Ruff, pyright, pytest. Packaged as a single container — no local Python required.

## Prerequisites

- Docker.
- [`go-task`](https://taskfile.dev).
- The shared `tbc-localdev-infra` stack (Postgres 17, Traefik with TLS for `*.localhost`) checked out at `~/projects/tbc-localdev-infra`. `task up` starts it; `task infra:up` / `task infra:down` drive it directly.

## Quick start

```sh
# Bring up the shared infra if it isn't already.
task infra:up

# Create the upmovies + upmovies_test databases on the shared Postgres.
cp .env.example .env   # first time only
task db:init

# Build and start the container (also starts the shared infra), then migrate.
task build
task up
task migrate
docker compose restart upmovies-backend   # first run only: it booted before the schema existed

# Verify.
curl -sk https://api.upmovies.localhost/healthz   # -> {"status":"ok"}
task test
```

`task -l` lists every target.

Hooks: `pre-commit install` once. Every hook runs inside the container (`docker compose exec -T upmovies-backend …`), so the container must be up to commit.

## Development

Everything runs inside the container via `task`:

| Task | Purpose |
|---|---|
| `task up` / `task down` / `task build` | container lifecycle |
| `task logs` | stream container logs |
| `task shell` | bash inside the container |
| `task test` | full pytest suite |
| `task lint` / `task format` | `ruff check` / `ruff format` |
| `task typecheck` | `pyright` |
| `task coverage` | pytest with coverage |
| `task migrate` | `alembic upgrade head` |
| `task makemigration -- "msg"` | autogenerate a new migration |

Source is bind-mounted into the container so uvicorn's `--reload` picks up edits without a rebuild. Dependencies install from `uv.lock`: after a `pyproject.toml` change, run `task lock`, then `task build`.

## Errors and observability

Two signals, one build identity (the ops repo's instrumentation contract, NEU-1590):

- **Errors** from the API and the scheduled tasks go to Bugsink through the Sentry SDK (`SENTRY_DSN` holds the Bugsink DSN; unset disables it). Bugsink alerts Discord `#alerts` on a new issue, a regression or an unmute. A scheduled task reports crashes only (ADR-0025): see below.
- **Traces, logs and metrics** go to SigNoz through `opentelemetry-instrument` (the Dockerfile's `CMD`).

Sentry's `release` and `environment` are read back from OTel's own resource attributes in `src/upmovies/telemetry.py`, so the two always agree: `release == service.version == SOURCE_COMMIT` and `environment == deployment.environment`. When the resource attributes carry a value it is used verbatim, `unknown` included, so Sentry never reports a build SigNoz did not. Without them (local dev) the fallbacks are `SOURCE_COMMIT`, then `GIT_SHA`, then `unknown` for the release, and `ENVIRONMENT`, then `development` for the environment. Startup logs the resolved pair beside what OTel was given — this line is the config dump to check after a deploy:

```
INFO upmovies.telemetry: telemetry: release=<sha> environment=production otel service.version=<sha> deployment.environment=production sentry=on
```

The deployment must set:

| Var | Where | Value |
|---|---|---|
| `OTEL_RESOURCE_ATTRIBUTES` | `docker-compose.prod.yml` | `service.name=upcoming-movies-backend,deployment.environment=production,service.version=${SOURCE_COMMIT:-unknown}` |
| `SOURCE_COMMIT` | Coolify ("Include SOURCE_COMMIT in build" on) | the deployed git SHA — without the setting it is the literal `unknown` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Coolify | the SigNoz collector on the internal Docker network; never committed |
| `SENTRY_DSN` | Coolify | the Bugsink project DSN |

`OTEL_SERVICE_NAME`, if set in Coolify, overrides the `service.name` above — leave it unset. Startup warns when `OTEL_EXPORTER_OTLP_ENDPOINT` is set but `OTEL_RESOURCE_ATTRIBUTES` is missing `service.version` or `deployment.environment`.

**Scheduled tasks.** `python -m upmovies.pipeline_run <mode>` opens one span per task, and a stage that crashes is recorded on it (`span.record_exception()`, then an error status) before its run row is finalized `failed`. That span exports only when the Coolify task's command is wrapped the way the Dockerfile's `CMD` is — `opentelemetry-instrument python -m upmovies.pipeline_run <mode>`; unwrapped, it is a no-op and the Uptime Kuma deadman remains the only signal.

A scheduled task also initialises the SDK, with the same release and environment as the API, and reports **one event per crashed run**: a stage that is finalized `failed` because it raised, or an exception that escapes the task. Each event is tagged `pipeline_mode=<mode>`. The per-item failures the pipelines log and carry on past are breadcrumbs, never events, and a stage that ends `failed` on a threshold (no crash) is not an event either — the deadman and SigNoz cover those (ADR-0025). To prove the path after a deploy:

```
python -m upmovies.pipeline_run raise-test-error
```

It raises `PipelineTestError`, which should appear in Bugsink as its own issue, tagged `pipeline_mode=raise-test-error`.

**Test error.** `POST /admin/debug/raise-test-error` (bearer `ADMIN_TOKEN`) is the `upmovies.debug.raise-test-error` trigger: it raises `TelemetryTestError`, answers 500, and should appear in Bugsink as its own issue and then in Discord `#alerts`. Against production:

```sh
curl -s -X POST https://api.backlotter.com/admin/debug/raise-test-error -H "Authorization: Bearer $ADMIN_TOKEN"
```
