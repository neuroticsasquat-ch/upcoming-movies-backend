# Upcoming Movies Backend — Agent Guide

## Spec and plan directory

`specs_dir: docs`

## How to work in this repo

**Everything runs inside the Docker container via `task`.** Never run `pytest`, `ruff`, `pyright`, `alembic`, or `python` on the host. Source is bind-mounted; dep changes (`pyproject.toml`) need `task build`.

## Production deployments

Production deploys are **not** triggered by `task` commands. The flow is:

1. Open a PR and merge to `main`.
2. Coolify builds and deploys the new image automatically.
3. To run a one-off script in production, exec into the running Coolify container and run it there:

   ```bash
   # example: find the container name first
   docker ps --filter "label=coolify.managed=true" --filter "ancestor=upmovies-backend"
   docker exec -it <container> python scripts/backfill_credit_removals.py
   ```

   (Exact container access may vary by Coolify setup; use the Coolify UI or host SSH as needed.)

### Coolify scheduled tasks

Seven slots run `python -m upmovies.pipeline_run <mode>` in the deployed container, each a separate
process with its own healthchecks.io deadman (`HEALTHCHECK_*_URL`):

| Mode | Cadence | What it does |
|---|---|---|
| `hourly` | hourly | the light feeds pass (`per_film=false`) |
| `sweep` | daily, ~2h ahead of `daily` | the undated-film sweep (ADR-0013) |
| `daily` | daily | tmdb → feeds(per-film) → link → synthesize, fail-fast |
| `providers` | daily, next to the sweep | the D-27 watch-provider poll, then the D-35 video poll (NEU-1374, NEU-1385) |
| `notify` | daily, **after** `daily` | the M7 decision pass: queues digest rows, sends nothing (D-31, NEU-1379, ADR-0021) |
| `digest daily` | daily, **after** `notify` | the daily digest: one mail per `digest_cadence = daily` user, with the slate on `SLATE_WEEKDAY` (D-33, DC-2, NEU-1381/1462) |
| `digest weekly` | weekly on `SLATE_WEEKDAY`, **after** `notify` | the weekly digest with the "your slate" section, for `weekly` users — the default (D-33, NEU-1381) |

**`providers` is a new slot and must be added in the Coolify UI** — nothing in the repo creates
it, so merging this leaves the poll never running, with no failing check to say so. Put it beside
the sweep rather than inside the daily chain: it shares the sweep's reasons for staying out (a
TMDB hiccup in a long catalog pass must not abort feeds, link and synthesize), and it reads a
working set the sweep has already dropped — films *past* their theatrical release. Set
`HEALTHCHECK_PROVIDERS_URL` in the same edit; unset, the pings are a silent no-op and a poll that
stops running is invisible. The `PROVIDER_POLL_*` window is seeded in `docker-compose.prod.yml`
and turned in the UI, per the gotcha below.

**The `providers` slot runs two passes, not one (NEU-1385).** The watch-provider poll and
then the video poll, over the same scoped set and under the same run row and deadman — so
there is no second slot to create and no new environment variable, and `ingest_run.detail`
carries a `videos:` clause beside the `providers:` one. Two consequences worth knowing:

- **It doubles the slot's TMDB traffic**, one extra request per film in the set, and the two
  passes share this process's one outbound window (see the rate-limiter gotcha below), so the
  slot takes roughly twice as long as it did. Watch the deadman's grace period after merging.
- **The first run after deploy cards nothing and that is correct.** Every film's first video
  read is its baseline (ADR-0014) — it records whatever TMDB already holds and stays silent,
  so a catalogue with years of trailers behind it does not empty itself onto the feed. Expect
  a large `recorded` with `0 carded` and a `baselined` that matches the number polled; real
  trailer cards start on the second run. `film.videos_observed_at` is the marker, and it is
  deliberately never reset.

**`notify` is a new slot and must be added in the Coolify UI**, on the same terms as
`providers` above and with `HEALTHCHECK_NOTIFY_URL` set in the same edit. Three things are
specific to it:

- **Order matters.** It reads the events the daily chain publishes, so it has to run *after* that
  chain, not beside it. It is deliberately not a fifth stage of the chain: the chain is fail-fast,
  and a link-stage outage must not mean nobody hears about the release dates the tmdb stage did
  card.
- **The first run queues nothing, by design.** The window is "published since the last
  *successful* notify run", and on a cold start there is no such run — so the first one
  establishes the watermark and queues nothing rather than queueing the entire back catalogue.
  Schedule it before announcing anything to users, and expect the slot's first green tick to
  report `cold start`.
- **This slot sends no mail** (ADR-0021, NEU-1470). It decides and writes `queued` digest rows;
  the digest slots below mail them. So it needs no `MAIL_*` configuration and is exempt from
  the mail guard (`_NO_MODEL_CALL_MODES`), like the sweep and the poll. There is no alert mail
  and no Web Push any more: the digest is the only delivery.

**`digest daily` and `digest weekly` are two more slots to add in the Coolify UI** (NEU-1381),
with `HEALTHCHECK_DIGEST_DAILY_URL` and `HEALTHCHECK_DIGEST_WEEKLY_URL` set in the same edit —
two checks, because a healthchecks.io check has one schedule. They are the slots that send
mail, so they carry the mail prerequisites:

- **`MAIL_PROVIDER=resend` plus its key and `MAIL_FROM` have to be real** before the slots are
  turned on — with the default `noop` provider a run goes green and transmits nothing, which is
  exactly what a staging environment wants and exactly what production must not be left on.
  `PUBLIC_BASE_URL` and `TMDB_IMAGE_BASE` are load-bearing too: a mail carries absolute links
  and absolute image URLs, with no page around them to resolve a relative path against.
- **A `failed` notification row is terminal.** The sender reads only `queued` rows and the
  decision pass will not re-queue them, so rows lost to a provider refusal need a hand-written
  re-queue (`UPDATE app.notification SET status = 'queued' WHERE …`) to go out. The blast radius
  is bounded by `INGEST_CONSECUTIVE_FAILURE_THRESHOLD`: that many consecutive refusals abort the
  send and fail the run, so a dead provider costs that many users rather than the whole backlog,
  and the deadman goes red instead of green.

And specific to their schedule and contents:

- **Run them after `notify`, on the same day.** The digest mails the `digest` rows the
  decision pass queued; a slot that runs before it mails yesterday's.
- **The weekly slot must run on `SLATE_WEEKDAY` (DC-2, NEU-1462).** That setting (default
  `thursday`, seeded in `docker-compose.prod.yml`) is the product's one slate day: the
  `digest daily` slot reads it and puts the slate in front of a daily reader's cards on that
  weekday — and mails a daily reader with an empty queue and a non-empty slate, as the weekly
  does. The weekly slot always carries the slate whatever day it runs, because the repo cannot
  see the Coolify schedule, so nothing fails if the two disagree: daily and weekly readers
  simply get their slates on different days. If the weekly slot is scheduled on another day,
  move it, or set `SLATE_WEEKDAY` to match in the Coolify UI (the compose value is a seed, per
  the gotcha below). Keep the day fixed once chosen: the slate's `new` / `moved` markers look
  back seven days, which is "since the previous slate day" only while the slate runs weekly on
  one weekday.
- **Nothing here has a watermark.** The backlog is the `queued` rows, so a failed run leaves
  exactly what did not go out for the next slot, and a first run is not a cold start — it
  mails whatever the notify pass has queued since it was turned on.
- **`digest_cadence = off` rows accumulate.** The decision pass keeps queueing for a user who
  has turned the digest off, and neither slot reads them; switching back to `weekly` gets
  everything since in one mail. Nothing prunes that backlog.
- **`API_BASE_URL` must be the API's public origin (DC-10, NEU-1463).** Every digest carries
  `List-Unsubscribe: <{API_BASE_URL}/digest/unsubscribe/{token}>` plus RFC 8058's one-click
  `List-Unsubscribe-Post`, and a mailbox provider POSTs to that URL from its own servers. Set
  it in the Coolify UI (`https://api.backlotter.com`, no trailing slash) and confirm it with
  `printenv`. Boot validation refuses `MAIL_PROVIDER=resend` on the code default
  (`http://localhost:8000`), but `docker-compose.prod.yml` seeds the prod origin, so in prod
  that guard never fires — a wrong Coolify value is caught by nothing. The route is public,
  answers `404` for an unknown token, and sits on its own `digest_unsubscribe` rate bucket,
  kept wide (`300/300`) because the one-click POSTs arrive from a mailbox provider's few
  shared IPs. The send creates a settings row for a reader it is about to mail who has none —
  the token lives there — so rows appear for weekly readers who never opened their settings.

Scripts that need to run in production must be copied into the image. Add `COPY scripts/ scripts/` to the `Dockerfile` for both `dev` and `prod` targets; otherwise the file is only available in local dev via bind-mount.

## Commands

| What | Command |
|------|---------|
| Build image | `task build` |
| Start (with --reload) | `task up` |
| Stop | `task down` |
| Shell in container | `task shell` |
| Full test suite | `task test` |
| Unit tests only | `task test:unit` |
| Integration tests | `task test:integration` |
| Lint | `task lint` |
| Format | `task format` |
| Typecheck | `task typecheck` |
| Coverage | `task coverage` |
| Run migrations | `task migrate` |
| New migration | `task makemigration -- "message"` |
| Prod DB refresh (local) | `task db:refresh` |
| Release notes for a tag (host) | `task release-notes -- v0.4.1` |

Before claiming work done: `task test && task lint && task typecheck` must all pass. Run `task format` first (ruff also reformats).

## Architecture

Single-container FastAPI service. Python 3.13, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2, httpx. Ruff + pyright + pytest. `uv` for package management.

### Layout (`src/upmovies/`)

- `app/` — auth/accounts (models, repos, services, routers)
- `catalog/` — Film/TMDB spine (models, TMDB client, upsert, seed grades)
- `news/` — Story ingestion, feeds (static `FEED_SOURCES`), fetcher
- `ingest/` — run tracking (`IngestRun`), TMDB ingest, sweep
- `public/` — API surfaces: feed (flat + grouped), film detail, calendar, sitemap
- `routers/` — FastAPI routers (public, admin, auth, etc.)
- `llm/` — LLM gateway, adapters (Anthropic, DeepInfra, DeepSeek), pricing
- `link/` — Story→film linking, retrieval, clustering, source gate
- `synthesize/` — Event summarization

DB split into Postgres schemas: `app`, `catalog`, `news`, `ingest`. Tests use `create_all` from models; prod uses Alembic migrations. The suite proves the two agree: `tests/integration/test_migrations.py` builds a scratch DB with `alembic upgrade head`, diffs it against the `create_all` schema, and round-trips the head revision.

### Key patterns

- **Layering:** routers → services → repos → models. Callers own the transaction.
- **Admin auth:** `require_admin` (bearer token, machine-facing) vs `require_current_admin` (session cookie, human-facing).
- **Ingestion:** pipelines take `(session_factory, run_id, …)`, commit per item. Background tasks `asyncio.create_task` with their own session; always finalize as `failed` on crash.
- **LLM gateway:** resolves provider per stage (not per model). Never falls back. Validated at startup — misconfiguration kills the container.
- **Candidate retrieval:** lexical-only (no model call). Squash-fold + tokenization matching. `T=0.5`, `K=47`.

### Terminology (context-sensitive, enforced in code)

- **provenance:** `"story"` or `"catalog"` — where an event was *born*, never mutated when a story attaches later.
- **news_backed:** `EXISTS(event_story)` — whether *any* of a film-day's visible events has a linked story. Deliberately NOT `provenance`.
- **Catalog-sourced event:** created from TMDB field change with no story. Has deterministic summary (`model="deterministic"`) and empty `sources`.
- **Total stage failure:** stage produced nothing at all — the pipeline aborts. Distinguished from partial failure (survivors committed, run succeeds).

## Testing

- Pytest-asyncio in `auto` mode (no `@pytest.mark.asyncio` needed). Session-scoped fixtures.
- HTTP mocking via **respx** — never hit the live network.
- Integration tests use `session` fixture against test DB. Re-read after Core-level upserts with `execution_options={"populate_existing": True}`.
- Running a single integration test file in isolation errors (pytest-asyncio quirk). Run the whole suite or scope to directory.
- Stale container → async-fixture errors: `docker compose -f ../docker-compose.yml restart api`.

## Gotchas

- **`db:refresh` silently reverts migrations.** Restores catalog/news/ingest from prod but leaves `app` alone. Alembic version lives in `app`, so `alembic current` still reads head while tables are gone. Re-apply with `alembic stamp <prod's rev> && task migrate`.
- **Coolify shadows compose fallbacks:** a `${NAME:-default}` in compose is a seed, not a runtime default. After first deploy, Coolify stores the value and edits to the fallback are silent no-ops in prod. Change the value in the Coolify UI and restart.
- **Deploy checklist for tuned constants** (T, K, dormancy, `SWEEP_CREDIT_QUARANTINE_HOURS`, `SWEEP_STORY_CONFIRM_DAYS`, the three `SWEEP_SANITY_*`, `PROVIDER_POLL_MAX_AGE_DAYS`, etc.): change code default → change `docker-compose.prod.yml` → edit Coolify UI → verify with `printenv` on the running container.
- **`PROVIDER_POLL_MAX_AGE_DAYS` is two things at once** (D-46, NEU-1417): the provider/video poll's age ceiling **and** the alert window's width, so it sets how long a follow keeps covering a film after release. It went 200 → 365 in code; until the Coolify value is flipped to match, prod runs the 200-day window and indirect followers miss late streaming debuts.
- **`SWEEP_CREDIT_QUARANTINE_HOURS` must stay at least 48h under `SWEEP_EVENT_LOOKBACK_DAYS`
  (NEU-1368, NEU-1401, ADR-0017 D-3).** In hours: 72 against a **120h ceiling** (7 days = 168,
  minus 48). The
  attachment hold has no queue table — the rolling lookback *is* the queue — so a hold that
  reaches the window means every attachment ages out before it is eligible and the credit half
  quietly stops carding. The ceiling sits 48h below the window rather than 1h below it because the
  hold is only ever *observed* at a sweep pass: eligibility falling just after a pass waits for the
  next one, so the **effective hold** runs up to a sweep period past the number configured (48h =
  one daily pass plus one skipped or shifted one; NEU-1372 measured a nominal 72h holding for
  ~94h). `validate_sweep_configuration` refuses the boot for that reason — in `pipeline_run` only,
  not the API, so a bad value fails the **hourly task** (and its healthchecks.io deadman) within
  the hour rather than taking the site down; its message names the ceiling and the lookback that
  would admit the value. Raising the window means raising `SWEEP_EVENT_LOOKBACK_DAYS` *first*, in
  the same Coolify edit; both are seeded in `docker-compose.prod.yml`. `0` disables the hold and is
  exempt. `SWEEP_CREDIT_DWELL_DAYS` rounds up the same way but is deliberately **not** guarded —
  the removal backfill can re-read what a mis-tuned dwell loses.
- **The three `SWEEP_SANITY_*` refuse `0` (NEU-1370, ADR-0017 D-8).** `MAX_FILMS_PER_DAY=20`,
  `POSTHUMOUS_YEARS=2`, `MIN_AGE_YEARS=3`, all `ge=1` in `config.py` — unlike the two gates
  above, none has a coherent "off": a burst bar of 0 holds every attachment ever made, and a
  zero-year date bar holds every credit of everyone TMDB has a date for. A `0` in the Coolify
  UI therefore fails the **hourly task** at boot, not the site. Turning a check off is a code
  change, deliberately.
- **A held attachment is invisible on the feed but visible on `/admin/credit-holds`.** If a
  real beat is missing, check the open holds before reaching for the sweep logs: a
  `deceased` or `implausible_age` hold never clears on its own, and
  `POST /admin/credit-holds/{id}/release` is the only way out other than the change ageing out
  of `SWEEP_EVENT_LOOKBACK_DAYS`. The `holds:` clause on the sweep detail line carries the
  per-pass counts.
- **Rate limiter rollout is three deploys, in order (NEU-1344, spec §5).** `RATE_LIMIT_PUBLIC_ENABLED`
  ships `false` and must stay false until the SSR Worker signs its requests: the site is rendered on
  a Cloudflare Worker, so until then every anonymous visitor reaches the API from a handful of shared
  egress IPs and one bucket throttles all of them at once. (1) Deploy the backend — auth buckets
  live, public bucket inert, `--proxy-headers` on. (2) Set `SSR_ORIGIN_SECRET` in the Coolify UI
  **and** as the Wrangler secret of the same name, then deploy the frontend. (3) Only then set
  `RATE_LIMIT_PUBLIC_ENABLED=true` in the Coolify UI and restart. Both are Coolify UI changes, not
  compose edits — the fallbacks in `docker-compose.prod.yml` are seeds, per the gotcha above.
- **Turning the public bucket on meters the calendar feed too (NEU-1383).**
  `GET /calendar/{token}.ics` sits in the `public` bucket, and unlike every other route in it the
  callers are not browsers: a subscribed feed is fetched by Google Calendar, Apple and Outlook on
  their own schedules, from *their* shared egress pools rather than the subscriber's device. So
  one bucket can hold many users' calendar clients, and `SSR_ORIGIN_SECRET` does not help — those
  fetchers do not go through the SSR Worker and cannot sign anything. Inert while
  `RATE_LIMIT_PUBLIC_ENABLED=false` (step 3 of the rollout above); when that step comes, check
  `RATE_LIMIT_PUBLIC_*` against how many subscribers a single calendar provider may be polling
  for before assuming the browser-shaped numbers fit.
- **The outbound TMDB rate limiter is per process, not per deployment (NEU-1399).** Clients
  built with `TMDBClient.from_settings(settings)` share one `RateLimiter`, so the API process's
  concurrent consumers — a Letterboxd import per upload (`routers/imports.py`), the
  `/admin/ingest/*` triggers that spawn stage runners in-process, the TMDB account import when
  it lands — spend a single `TMDB_RATE_LIMIT_REQUESTS` budget between them rather than one
  each. **Build production clients with the classmethod**; the raw constructor still gives a
  window of its own (that is what tests want), and `tests/unit/ingest/tmdb/test_client.py`
  fails if a new `src/` or `scripts/` call site uses it. What this does *not* bound is the
  deployment: each `python -m upmovies.pipeline_run` invocation is a separate process (ADR-0003)
  with its own window, as is `scripts/probe_undated_candidates.py`, so the API plus a running
  daily chain can still ask TMDB for more than the configured rate — bounded by the number of
  live processes, not by the number of clients. Cross-process limiting is deliberately not
  built: it means a shared Postgres or Redis round-trip on the hot path of every TMDB request,
  and the failure mode here is politeness, not correctness (the client honours `Retry-After` on
  429 without spending its retry budget). Not to be confused with the *inbound* per-IP limiter
  in `app/rate_limit.py`, which is a different mechanism with different settings.
  Note that `NEU-1356-letterboxd-import.md` §4 originally asserted the limiter was already
  process-wide *and* that an import shared the daily chain's budget; it carries a dated
  correction — the chain was always a separate process, so no process-wide limiter reaches it.
- **Long-running container holds the env it was created with.** After any env change: `docker compose -f ../docker-compose.yml up -d --force-recreate api` and `printenv` to confirm.
- **Migrations:** add model column first (tests get it via `create_all`), then `task makemigration -- "msg"`, review, `task migrate`. Autogenerate never emits `CheckConstraint` — write every `ck_*` by hand, and give any hand-named constraint the same `name=` in the model: the parity test (`tests/integration/test_migrations.py`) compares names, so a mismatch fails `task test`.


## Sibling repo

The frontend lives at `../frontend`. Read its `AGENTS.md` before working on frontend code.

## Conventions

- Type hints: `X | None`, `X | Y` (no `Optional`/`Union`). No `from __future__ import annotations`.
- Ruff (line length 100, rules E,F,W,I,B,UP). Use `import x as x` re-export in `__init__.py` to avoid F401.
- Commits: Conventional Commits with a scope and a trailing Linear ID: `feat(auth): add X (NEU-123)`. PR titles same format.
- The scope is the component (`auth`, `mail`, `retrieval`, …) and drives release notes: `cliff.toml` groups `RELEASE_NOTES.md` by it, and scopeless commits fall under "General".
- Release notes: tag first, then `task release-notes -- v0.4.1`. Runs on the **host** (git-cliff needs git history and isn't in the image) — the one documented exception to the container rule. It prepends a section; never rebuild the file wholesale, never call `git-cliff` directly.
- Branch: per ticket using Linear's generated branch name.
