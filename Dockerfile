FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./


FROM base AS dev

RUN uv pip install --system --no-cache ".[dev]"

COPY src/ src/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

EXPOSE 8000

CMD ["uvicorn", "upmovies.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM base AS prod

RUN uv pip install --system --no-cache .

COPY src/ src/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

EXPOSE 8000

# opentelemetry-distro defaults the OTLP protocol to gRPC, but we install only
# the HTTP exporter. Pin http/protobuf so the absent grpc exporter is never
# looked up. Endpoint + service.name come from the deploy env (Coolify).
ENV OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf

# Run migrations on startup, then exec uvicorn (wrapped by opentelemetry-instrument
# to auto-instrument FastAPI/SQLAlchemy/asyncpg/httpx) so signals reach the server.
CMD ["sh", "-c", "alembic upgrade head && exec opentelemetry-instrument uvicorn upmovies.main:app --host 0.0.0.0 --port 8000"]
