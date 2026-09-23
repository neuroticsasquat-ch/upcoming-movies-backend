# NEU-1451 — `GET /me/follows` stays unpaginated: a measured ceiling, gzip, and a signal

**Ticket:** [NEU-1451](https://linear.app/neuroticsasquatch/issue/NEU-1451/backend-get-mefollows-is-unpaginated-an-imported-library-costs)
**Project:** bl: Entity Follows · **Milestone:** none (filed outside the milestones as a follow-up)
**Blocked by:** nothing · **Blocks:** nothing
**Related:** NEU-1439 (headline dates on the list, PR #361), NEU-1440 (`last_activity_at`, PR #362), NEU-1449 (two-phase import, PR #366)
**Decisions honoured:** EF-15 (one flat list, client-side filter and sorts — kept as is), EF-21 (only in-window films are import candidates), D-40 (nothing prunes follows)
**Target repo:** upcoming-movies-backend · **Base branch:** `release/v1.0.0`
**Frontend:** no change. The payload shape, the flat list and every consumer of `useFollows` are untouched.
**Glossary:** no new term; `CONTEXT.md` unchanged.

## Decision (planned 2026-09-23 with Tom)

**Option 4 of the ticket — measure and accept — with three concrete outputs.** The route is not
paged, the client-side filter stays, and nothing about the follows page changes. What ships:

1. **The measured ceiling written down** in `routers/follows.py` (the ticket's own ask) and as a
   §8 open item in the project spec, with the trigger that reopens the paging design.
2. **Response compression app-wide** (`GZipMiddleware`), because the payload — not the query
   time — is what a user on a real connection feels, and gzip takes it down eightfold.
3. **A threshold warning** from the list route when an account passes 2,000 follows, so the
   ceiling produces a signal in production instead of the ticket's "current silence".
4. **The bench script kept in the repo** (`scripts/bench_follows.py`) so the numbers can be
   re-taken by anyone rather than rebuilt from a recipe in a Linear comment.

### Why accept

Two facts found during planning outweigh the ticket's "10,000 is reachable" premise:

- **An import cannot create a follow outside the alert window.** EF-21 lists a matched film
  outside the window with `skip_reason = 'outside_window'`, and `ingest/imports/review.py::
  confirm` writes follows only for `import_candidate_repo.selectable_film_ids`, which ignores
  skipped rows rather than honouring them. So the 5,000-row importer caps bound the *file*, not
  the follows. The dev catalog holds roughly 9,500 in-window films in total; 10,000 title
  follows would mean following essentially every film the site can alert on. A heavy importer
  lands at hundreds to low thousands. Entity follows have no bulk writer at all (NEU-1448:
  imports write title follows only).
- **Paging the route costs more than EF-15's filter.** `api/me.ts::useIsFollowing` and
  `useFollow` answer "do I follow X?" by scanning the whole cached list, and every follow
  button in the app reads them — the film page, the entity pages, the onboarding tiles, the
  search box on the follows page itself — as do the counts on `/welcome`. Any paged design
  needs a second cheap keys view plus a rewrite of `useToggleFollow`'s optimistic update, i.e. a
  coupled cross-repo change, for a load no account can produce.

The re-measurement after NEU-1440 (below) confirms the shape the ticket's first comment
predicted — four linear passes, `last_activity_at` now the largest — and puts the realistic
maximum at about 200 ms and under 1 MB uncompressed. That is acceptable, and now it is measured.

## The numbers (post-NEU-1440, 2026-09-23)

Dev container, Postgres in Docker, warm cache, no network. One entitled user, all title follows,
each film with one `US` / `release_type=3` future release row and **2 published visible cards**
(`release_date` region `US` and `trailer`, each with an `event_summary` row). Median of 3.

| Follows | follow rows | entity labels | headline_release | last_activity | service total | DTO + JSON | payload | gzip |
|---|---|---|---|---|---|---|---|---|
| 1,000 | 7 ms | 14 ms | 18 ms | 64 ms | **113 ms** | 9 ms | 0.32 MB | — |
| 5,000 | 36 ms | 51 ms | 115 ms | 154 ms | **348 ms** | 62 ms | 1.58 MB | 0.19 MB (17 ms) |
| 10,000 | 91 ms | 107 ms | 260 ms | 233 ms | **694 ms** | 110 ms | 3.17 MB | — |

With **10 cards per film** at 10,000 follows the activity pass rises to 326 ms and the service
total to 803 ms; the other three lines are unchanged. Pre-NEU-1440 (ticket comment, 2026-09-22)
the totals were 52 / 274 / 570 ms, so `last_activity_at` roughly doubled the server time at
every size and is the line that grows with the event table rather than with the follow count.

Reading: 1k needs nothing; 2k is where the route passes ~150 ms and ~0.6 MB uncompressed (the
warning threshold); 5k is noticeable but survivable, and 0.19 MB on the wire once gzipped; 10k
is not defensible and not reachable through any writer the product has.

## What to build

### 1. `GZipMiddleware` in `create_app` (`src/upmovies/main.py`)

```python
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1024)
```

- Registered beside `CORSMiddleware`. Default `compresslevel` (Starlette's 9) is fine at these
  sizes; use 6 if the 10k bench shows it costing more than ~50 ms.
- Applies to every response that carries `Accept-Encoding: gzip` and is at least 1 KB, so the
  feed, timeline, calendar, `/events` and the iCal feed benefit too. No route streams
  (`StreamingResponse` is unused), and no test pins `Content-Length`; httpx decodes gzip
  transparently, so the existing suite needs no change.
- **BREACH note, for the docstring/comment:** the one response that carries a secret is
  `GET /me` (the CSRF token in its body, NEU-1382). BREACH also needs attacker-controlled input
  reflected into that same response, and `/me` reflects nothing from the request — its body is
  the account row. `minimum_size=1024` additionally keeps that small body out of the compressor
  as belt and braces. Say this where the middleware is registered so nobody lowers the floor
  without reading it.

### 2. The threshold warning (`app/services/follow_service.py`)

```python
FOLLOWS_WARN_THRESHOLD = 2_000
"""Follow count past which `list_follows` logs — see the module docstring and NEU-1451."""
```

- In `list_follows`, after `follow_repo.list_for_user`, if `len(follows) > FOLLOWS_WARN_THRESHOLD`:
  `log.warning("user_id=%s has %d follows, past the %d-row ceiling GET /me/follows is measured
  for (NEU-1451)", user.id, len(follows), FOLLOWS_WARN_THRESHOLD)`. One line per request over
  the threshold; that account is the signal, and a follows-page load is the only time it fires.
- A module constant, not a setting: it is documentation with a side effect, the same way
  NEU-1417 made the alert-window status term a constant. `logging.getLogger(__name__)` as the
  other services do.
- Test: a unit-level test that seeds `FOLLOWS_WARN_THRESHOLD + 1` follows (monkeypatch the
  constant down to, say, 3 rather than inserting 2,001 rows) and asserts the warning with
  `caplog`; and that the threshold itself does not fire (`== threshold` is silent).

### 3. The docstrings

- `routers/follows.py::list_follows` (the ticket's explicit ask): the table above in prose or
  as a compact table, the four-linear-passes shape, the EF-21 bound and why it makes 10k
  unreachable, the 2,000 warning, the gzip ratio, and the pointer to `scripts/bench_follows.py`
  and to this spec. This is the thing the next person reads before re-deriving any of it.
- `follow_service.list_follows`: one sentence pointing at the router docstring and the
  constant; do not duplicate the table.

### 4. `scripts/bench_follows.py`

The script used for the table, kept. Shape (the working version from this session is in the
appendix; tidy it to the repo's script conventions — module docstring with the run line, as
`scripts/measure_title_filter.py` does):

- Runs inside the api container: `python scripts/bench_follows.py 5000 [cards_per_film]`.
- **Its own database.** Derives the URL from `TEST_DATABASE_URL` with the database name replaced
  by `app_bench`, and creates that database if it is missing (connect to the maintenance DB,
  `AUTOCOMMIT`, `CREATE DATABASE`). Never `app_test`: pytest owns it and an orphaned run
  deadlocks it (`AGENTS.md`).
- Drops and recreates the four schemas, `citext` + `pgcrypto`, `Base.metadata.create_all`, then
  **Core-inserts** (the ORM unit-of-work flush at 10k rows dominates the measurement): one
  entitled user, N films with `slug`, `origin_country=['US']` and one US wide future
  `film_release_date`, N title follows with `source='tmdb_import'`, K published visible events
  per film with `event_summary` rows, then `ANALYZE`.
- Times, in **separate windows**: the four passes individually (`follow_repo.list_for_user`,
  `follow_repo.entity_labels`, `catalog.headline_release.headline_releases`,
  `follow_service._last_activity`), then `follow_service.list_follows` whole, then the
  `FollowOut` build and `FollowListResponse.model_dump_json()` separately, then gzip level 6 of
  the body. Anything placed between two timestamps lands in the wrong column. Median of 3.
- A dev-only measurement. Both Dockerfile targets `COPY scripts/ scripts/`, so it ships in the
  image like every other script; that is harmless, since it only ever touches `app_bench`.

### 5. The record

- Project spec `docs/specs/bl-entity-follows-project-spec.md` §8 gains the open item (written
  during planning): the ceiling, the numbers, and the reopen trigger.
- **Reopen the paging design when any of these happens:** a write path that can create title
  follows outside the alert window (or bulk-create entity follows); the 2,000 warning firing in
  production; or a fifth linear pass being proposed for the route. The design, if it is ever
  needed, is the ticket's first option — `limit`/cursor with server-side `types`, `q` and
  `sort` on the list, plus a keys view for the follow buttons — using `upmovies/pagination.py`'s
  cursor codec; it needs a frontend ticket and a coupled deploy.

## Acceptance criteria

- [ ] `GET /me/follows` (and every JSON route) answers gzip-encoded when the client sends
      `Accept-Encoding: gzip` and the body is ≥ 1 KB; a request without the header gets an
      identity body; `GET /me` (under 1 KB) is never compressed. One router test covers the
      three cases via the middleware, not per route.
- [ ] `follow_service.list_follows` logs one WARNING with the user id and count when the follow
      count exceeds `FOLLOWS_WARN_THRESHOLD` (2,000), and nothing at or below it. Tested with the
      constant monkeypatched down.
- [ ] `routers/follows.py::list_follows`'s docstring carries the measured table, the EF-21
      bound, the threshold, the gzip ratio and the pointer to the bench script and this spec.
- [ ] `scripts/bench_follows.py` exists, runs against `app_bench`, and its module docstring says
      how to run it and what it reports. Running it at 1,000 reproduces the table's first row
      within noise.
- [ ] The frontend is untouched and the response shape is byte-for-byte the same uncompressed.
- [ ] Full backend suite green (pre-commit runs it; ~2 min).

## Out of scope / deferred

- Paging, server-side filter or sort, a keys view, and any frontend change (EF-15 stands).
- Capping follows at the source (the ticket's option 3): EF-21 already bounds imports better
  than a row cap would, and a manual follower is nobody's problem.
- Cheapening any single pass (dropping `headline_release`, caching `last_activity_at`): each is
  a third or less of the total and would not change the shape.
- Proxy-level compression in Coolify: unverifiable from the repo; the app-level middleware
  makes it unnecessary.
- Alerting on the warning line: it goes to the API's stdout like every other warning; wiring it
  to anything is a deployment question.

## Appendix — the session's bench script (working, untidied)

```python
"""Profile GET /me/follows at N title follows, post-NEU-1440 (adds last_activity_at).

Scratch DB `app_bench` (never app_test). Core inserts. Each film carries one US wide future
release row and K published, visible events (release_date region US + trailer) with summaries —
the shape of an imported library whose films have delivered a few cards each.

    python scripts/bench_follows.py 5000 [cards_per_film]
"""

import asyncio, gzip, os, statistics, sys, time, uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import upmovies.catalog.models  # noqa: F401
import upmovies.news.models  # noqa: F401
from upmovies.app.dto import FollowListResponse
from upmovies.app.models import Base, User
from upmovies.app.repos import follow_repo
from upmovies.app.services import follow_service
from upmovies.app.services.follow_service import _last_activity
from upmovies.catalog.headline_release import headline_releases

N = int(sys.argv[1])
K = int(sys.argv[2]) if len(sys.argv) > 2 else 2
RUNS = 3
url = os.environ["TEST_DATABASE_URL"].rsplit("/", 1)[0] + "/app_bench"
SCHEMAS = ("app", "catalog", "news", "ingest")


async def build(engine):
    async with engine.begin() as conn:
        for s in SCHEMAS:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE"))
        for s in SCHEMAS:
            await conn.execute(text(f"CREATE SCHEMA {s}"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.create_all)
    t = Base.metadata.tables
    user_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    films, rds, follows, events, summaries = [], [], [], [], []
    for i in range(N):
        fid = uuid.uuid4()
        films.append(dict(id=fid, tmdb_id=1_000_000 + i, slug=f"film-{i}", title=f"Film {i}",
                          origin_country=["US"], status="Post Production"))
        rds.append(dict(id=i + 1, film_id=fid, iso_3166_1="US", release_type=3,
                        release_date=now + timedelta(days=30 + (i % 300))))
        follows.append(dict(user_id=user_id, entity_type="title", entity_id=str(fid),
                            source="tmdb_import", created_at=now - timedelta(seconds=i)))
        for k in range(K):
            eid = uuid.uuid4()
            events.append(dict(id=eid, film_id=fid,
                               event_type="release_date" if k == 0 else "trailer",
                               confidence="confirmed", provenance="catalog", status="published",
                               region="US" if k == 0 else None,
                               occurred_at=now - timedelta(days=k + 1),
                               created_at=now - timedelta(days=k + 1, seconds=i)))
            summaries.append(dict(event_id=eid, summary=f"Card {k} for Film {i}", model="det",
                                  prompt_version="1", source_updated_at=now))
    async with engine.begin() as conn:
        await conn.execute(t["app.user"].insert(), [dict(
            id=user_id, email="bench@example.com", password_hash="x", display_name="b",
            entitled_until=now + timedelta(days=365))])
        await conn.execute(t["catalog.film"].insert(), films)
        await conn.execute(t["catalog.film_release_date"].insert(), rds)
        await conn.execute(t["app.follow"].insert(), follows)
        await conn.execute(t["news.event"].insert(), events)
        await conn.execute(t["news.event_summary"].insert(), summaries)
        await conn.execute(text("ANALYZE"))
    return user_id


async def measure(engine, user_id):
    from upmovies.routers.follows import _to_out
    out = {}
    async with AsyncSession(engine, expire_on_commit=False) as db:
        user = await db.get(User, user_id)
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
        out["rows"], out["labels"], out["headline"], out["activity"] = (
            t1 - t0, t2 - t1, t3 - t2, t4 - t3)
        db.expunge_all()
        t5 = time.perf_counter()
        rows = await follow_service.list_follows(db, user=user)
        t6 = time.perf_counter()
        items = [_to_out(r.follow, r.label, r.headline, r.last_activity) for r in rows]
        t7 = time.perf_counter()
        body = FollowListResponse(items=items).model_dump_json().encode()
        t8 = time.perf_counter()
        gz = gzip.compress(body, 6)
        t9 = time.perf_counter()
        out.update(db_total=t6 - t5, dto=t7 - t6, json=t8 - t7, gz_ms=t9 - t8,
                   bytes=len(body), gz=len(gz))
    return out


async def main():
    engine = create_async_engine(url)
    user_id = await build(engine)
    runs = [await measure(engine, user_id) for _ in range(RUNS)]
    med = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
    ms = lambda k: f"{med[k] * 1000:.0f} ms"  # noqa: E731
    print(f"N={N} events/film={K}")
    print(f"  rows {ms('rows')} | labels {ms('labels')} | headline {ms('headline')} | "
          f"activity {ms('activity')}")
    print(f"  service total {ms('db_total')} | dto {ms('dto')} | json {ms('json')} | "
          f"payload {med['bytes'] / 1e6:.2f} MB | gzip {med['gz'] / 1e6:.2f} MB in {ms('gz_ms')}")
    await engine.dispose()


asyncio.run(main())
```

The session's run created `app_bench` by hand (`psql -c "CREATE DATABASE app_bench"`) and
dropped it afterwards; the kept script should do the create itself.
