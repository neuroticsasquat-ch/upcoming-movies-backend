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
# pyright downloads its own prebuilt node (via nodeenv), and that binary links
# against libatomic, which python:*-slim does not ship. Without it the pyright
# hook dies with "libatomic.so.1: cannot open shared object file".
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
RUN uv pip install --system --no-cache ".[dev]"
# Bake the node download into the image so the first pre-commit run does not
# pay for it (and does not need the network).
RUN pyright --version
COPY src/ src/
COPY scripts/ scripts/
COPY alembic.ini alembic.ini
COPY migrations/ migrations/
EXPOSE 8000
CMD ["uvicorn", "upmovies.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


FROM base AS prod

RUN uv pip install --system --no-cache .

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
