FROM python:3.13-slim AS base

# UV_PROJECT_ENVIRONMENT: `uv sync` installs into the image's system Python, as
# `uv pip install --system` did, rather than a .venv.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install from uv.lock, the set CI tests, not from pyproject.toml's ranges: those
# resolve to whatever is newest at build time, so CI, dev and prod could each run
# different versions. --locked fails the build when the lockfile is stale, as it
# fails CI. --inexact keeps what the lockfile doesn't list -- pip and uv itself --
# in /usr/local.
COPY pyproject.toml uv.lock ./


FROM base AS dev
# pyright downloads its own prebuilt node (via nodeenv), and that binary links
# against libatomic, which python:*-slim does not ship. Without it the pyright
# hook dies with "libatomic.so.1: cannot open shared object file".
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --locked --inexact --no-cache --extra dev --no-install-project
# Bake the node download into the image so the first pre-commit run does not
# pay for it (and does not need the network).
RUN pyright --version
COPY src/ src/
COPY scripts/ scripts/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/
RUN uv sync --locked --inexact --no-cache --extra dev
EXPOSE 8000
# `--proxy-headers --forwarded-allow-ips=*` here for parity with prod below, where they are
# what makes `request.client.host` the caller rather than the proxy (NEU-1344).
CMD ["uvicorn", "upmovies.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--proxy-headers", "--forwarded-allow-ips", "*"]


FROM base AS prod

RUN uv sync --locked --inexact --no-cache --no-dev --no-install-project

COPY src/ src/
COPY scripts/ scripts/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

RUN uv sync --locked --inexact --no-cache --no-dev --no-editable

EXPOSE 8000

# opentelemetry-distro defaults the OTLP protocol to gRPC, but we install only
# the HTTP exporter. Pin http/protobuf so the absent grpc exporter is never
# looked up. Endpoint + service.name come from the deploy env (Coolify).
ENV OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf

# Run migrations on startup, then exec uvicorn (wrapped by opentelemetry-instrument
# to auto-instrument FastAPI/SQLAlchemy/asyncpg/httpx) so signals reach the server.
#
# `--proxy-headers --forwarded-allow-ips='*'` (NEU-1344): without them every request
# arrives as Traefik's address, which makes the per-IP rate limiter one global bucket and
# has been recording the proxy in `app.login_attempt.ip`. Trusting every hop is safe here
# because this port is reachable only over the Docker network — Traefik is the sole route
# in, so there is no path by which a client sets its own `X-Forwarded-For`.
CMD ["sh", "-c", "alembic upgrade head && exec opentelemetry-instrument uvicorn upmovies.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
