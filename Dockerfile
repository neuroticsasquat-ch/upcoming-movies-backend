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

# Run migrations on startup, then exec uvicorn so signals reach the server.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn upmovies.main:app --host 0.0.0.0 --port 8000"]
