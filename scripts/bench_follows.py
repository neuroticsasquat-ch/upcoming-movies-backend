"""Profile `GET /me/follows` at N title follows — the numbers in `routers/follows.py`'s
docstring (NEU-1451). Dev-only; it builds its own data and touches nothing else. Run in the
container:

    task shell
    python scripts/bench_follows.py 5000 [cards_per_film]

It works in its **own database**, `app_bench` (created on first run), derived from
`TEST_DATABASE_URL` — never `app_test`, which pytest owns and an orphaned run deadlocks. Each
run drops and rebuilds the four schemas, then Core-inserts one entitled user, N films (each with
one US wide future release row), N title follows, and `cards_per_film` (default 2) published,
visible cards per film with summaries — the shape of an imported library whose films have
delivered a few cards each. The ORM is kept out of the seed on purpose: its flush at 10k rows
would take longer than the thing being measured.

It reports, median of 3, each in its own timing window so nothing lands in the wrong column:
the four linear passes individually (follow rows, entity labels, `headline_release`,
`last_activity`), the service total, the `FollowOut` build and the JSON dump, and the payload
size raw and gzipped at the level `GZipMiddleware` uses."""

import asyncio
import gzip
import os
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import upmovies.catalog.models  # noqa: F401 — registers the tables `create_all` builds
import upmovies.news.models  # noqa: F401
from upmovies.app.dto import FollowListResponse
from upmovies.app.models import Base, User
from upmovies.app.repos import follow_repo
from upmovies.app.services import follow_service
from upmovies.app.services.follow_service import _last_activity
from upmovies.catalog.headline_release import headline_releases
from upmovies.routers.follows import _to_out

BENCH_DB = "app_bench"
SCHEMAS = ("app", "catalog", "news", "ingest")
RUNS = 3
GZIP_LEVEL = 6  # what `main.py` gives `GZipMiddleware`; keep the two in step


async def ensure_database(url: str) -> None:
    """Create `app_bench` if it is missing, from the server's maintenance database."""
    admin = create_async_engine(
        make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": BENCH_DB}
        )
        if not exists:
            await conn.execute(text(f"CREATE DATABASE {BENCH_DB}"))
    await admin.dispose()


async def build(engine: AsyncEngine, n: int, cards_per_film: int) -> uuid.UUID:
    async with engine.begin() as conn:
        for schema in SCHEMAS:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.create_all)

    tables = Base.metadata.tables
    user_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    films, release_dates, follows, events, summaries = [], [], [], [], []
    for i in range(n):
        film_id = uuid.uuid4()
        films.append(
            {
                "id": film_id,
                "tmdb_id": 1_000_000 + i,
                "slug": f"film-{i}",
                "title": f"Film {i}",
                "origin_country": ["US"],
                "status": "Post Production",
            }
        )
        release_dates.append(
            {
                "id": i + 1,
                "film_id": film_id,
                "iso_3166_1": "US",
                "release_type": 3,
                "release_date": now + timedelta(days=30 + (i % 300)),
            }
        )
        follows.append(
            {
                "user_id": user_id,
                "entity_type": "title",
                "entity_id": str(film_id),
                "source": "tmdb_import",
                "created_at": now - timedelta(seconds=i),
            }
        )
        for k in range(cards_per_film):
            event_id = uuid.uuid4()
            events.append(
                {
                    "id": event_id,
                    "film_id": film_id,
                    "event_type": "release_date" if k == 0 else "trailer",
                    "confidence": "confirmed",
                    "provenance": "catalog",
                    "status": "published",
                    "region": "US" if k == 0 else None,
                    "occurred_at": now - timedelta(days=k + 1),
                    "created_at": now - timedelta(days=k + 1, seconds=i),
                }
            )
            summaries.append(
                {
                    "event_id": event_id,
                    "summary": f"Card {k} for Film {i}",
                    "model": "deterministic",
                    "prompt_version": "1",
                    "source_updated_at": now,
                }
            )

    async with engine.begin() as conn:
        await conn.execute(
            tables["app.user"].insert(),
            [
                {
                    "id": user_id,
                    "email": "bench@example.com",
                    "password_hash": "x",
                    "display_name": "bench",
                    "entitled_until": now + timedelta(days=365),
                }
            ],
        )
        await conn.execute(tables["catalog.film"].insert(), films)
        await conn.execute(tables["catalog.film_release_date"].insert(), release_dates)
        await conn.execute(tables["app.follow"].insert(), follows)
        if events:
            await conn.execute(tables["news.event"].insert(), events)
            await conn.execute(tables["news.event_summary"].insert(), summaries)
        await conn.execute(text("ANALYZE"))
    return user_id


async def measure(engine: AsyncEngine, user_id: uuid.UUID) -> dict[str, float]:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        user = await db.get(User, user_id)
        assert user is not None
        today = datetime.now(tz=UTC).date()

        t0 = time.perf_counter()
        follows = await follow_repo.list_for_user(db, user.id)
        t1 = time.perf_counter()
        await follow_repo.entity_labels(db, [(f.entity_type, f.entity_id) for f in follows])
        t2 = time.perf_counter()
        await headline_releases(db, [uuid.UUID(f.entity_id) for f in follows], today=today)
        t3 = time.perf_counter()
        await _last_activity(db, user_id=user.id)
        t4 = time.perf_counter()

        # A cold identity map for the whole-service window, so it pays for its own row loads.
        db.expunge_all()
        user = await db.get(User, user_id)
        assert user is not None
        t5 = time.perf_counter()
        rows = await follow_service.list_follows(db, user=user)
        t6 = time.perf_counter()
        items = [_to_out(r.follow, r.label, r.headline, r.last_activity) for r in rows]
        t7 = time.perf_counter()
        body = FollowListResponse(items=items).model_dump_json().encode()
        t8 = time.perf_counter()
        compressed = gzip.compress(body, GZIP_LEVEL)
        t9 = time.perf_counter()

    return {
        "rows": t1 - t0,
        "labels": t2 - t1,
        "headline": t3 - t2,
        "activity": t4 - t3,
        "service": t6 - t5,
        "dto": t7 - t6,
        "json": t8 - t7,
        "gzip_time": t9 - t8,
        "bytes": len(body),
        "gzip_bytes": len(compressed),
    }


async def main() -> None:
    n = int(sys.argv[1])
    cards_per_film = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(database=BENCH_DB)
    await ensure_database(url.render_as_string(hide_password=False))

    engine = create_async_engine(url)
    try:
        user_id = await build(engine, n, cards_per_film)
        runs = [await measure(engine, user_id) for _ in range(RUNS)]
    finally:
        await engine.dispose()

    med = {key: statistics.median(run[key] for run in runs) for key in runs[0]}

    def ms(key: str) -> str:
        return f"{med[key] * 1000:.0f} ms"

    print(f"follows={n} cards_per_film={cards_per_film} (median of {RUNS})")
    print(
        f"  rows {ms('rows')} | labels {ms('labels')} | headline {ms('headline')} | "
        f"activity {ms('activity')}"
    )
    print(
        f"  service {ms('service')} | dto {ms('dto')} | json {ms('json')} | "
        f"payload {med['bytes'] / 1e6:.2f} MB | "
        f"gzip {med['gzip_bytes'] / 1e6:.2f} MB in {ms('gzip_time')}"
    )


if __name__ == "__main__":
    asyncio.run(main())
