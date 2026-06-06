# upcoming-movies-backend

FastAPI service backing the Upcoming Movies Tracker.

Stack: Python 3.13, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2, httpx, uv. Ruff, pyright, pytest. Packaged as a single container — no local Python required.

## Prerequisites

- Docker.
- [`go-task`](https://taskfile.dev).
- The shared localdev infra stack (Postgres 17, Traefik with TLS for `*.localhost`) running on the external `proxy` Docker network.

## Quick start

```sh
# Bring up the shared infra if it isn't already.
task infra:up

# Create databases and run migrations.
task db:init

# Build and start the container.
task build
task up

# Verify.
curl -sk https://api.upmovies.localhost/healthz   # -> {"status":"ok"}
task test
```

`task -l` lists every target.

> **Note:** `Taskfile.yml`, `docker-compose.yml`, and related infra files are added in a later task. The quick-start above reflects the intended workflow once those are in place.

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

Source is bind-mounted into the container so uvicorn's `--reload` picks up edits without a rebuild. Dependency changes (`pyproject.toml`) require `task build`.
