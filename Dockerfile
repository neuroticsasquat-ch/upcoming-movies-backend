# Reference backend Dockerfile. Lives at the root of every backend repo.
# The template's compose builds `target: dev`; your deploy builds `target: prod`.

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml ./


FROM base AS dev

RUN uv pip install --system --no-cache ".[dev]"

COPY . .

EXPOSE 8000

# --reload works because the compose mounts the repo over /app.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


FROM base AS prod

RUN uv pip install --system --no-cache .

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
