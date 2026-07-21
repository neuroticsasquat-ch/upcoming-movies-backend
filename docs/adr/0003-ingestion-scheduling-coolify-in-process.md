# Run ingestion as in-process Coolify scheduled tasks, not HTTP-polled GitHub Actions

**Status:** accepted

## Context

The daily and hourly ingestion pipelines were driven by GitHub Actions workflows
(`daily-pipeline.yml`, `hourly-feeds.yml`). Each `trigger_and_wait` step `POST`ed an
`/admin/ingest/<pipeline>` endpoint — which spawns a fire-and-forget `asyncio` background
task and returns a `run_id` immediately — then polled `GET /admin/ingest/<run_id>` every
30s until the run reached a terminal status, failing the job on `failed`/`cancelled` or a
poll-loop timeout.

This coupled the CI job's fixed poll budget to the pipeline's wall-clock. The `link` run
makes **two sequential Anthropic Message Batch calls** (link stage, then cluster stage),
each polling up to 60 min (`AnthropicClient.complete_batch` default `timeout=3600`). A
single `link` run can therefore take up to ~120 min server-side, but the daily workflow
polled the run for only 60 min (`seq 1 120` × `sleep 30`). The Message Batch API is
best-effort ("within 24h") — normally minutes, but when batches backed up the workflow
timed out while the server-side run kept going, marking the daily job failed two days
running with no ingestion-code change.

The polling design had a second flaw: the workflow triggered the four pipelines as
**independent** HTTP calls (`trigger_and_wait link || rc=1` then, unconditionally,
`trigger_and_wait synthesize`). Because each trigger only starts a background task, and the
steps ran back-to-back regardless of outcome, `synthesize` could begin while `link` was
still linking stories server-side — producing `summarized 0` and silently stale daily
summaries.

## Decision

Move both schedules off GitHub Actions to **Coolify scheduled tasks** that run the stages
**sequentially, in-process** inside the app container via a single command:
`python -m upmovies.pipeline_run {daily|hourly}`.

- The four stage bodies (client wiring + finalize-on-crash) previously inlined in
  `routers/ingest_admin.py`'s `_background_*` wrappers are extracted into shared
  `run_*_stage(run_id, settings)` helpers in `upmovies.pipeline_run`. The trigger endpoints
  now spawn those same helpers, so there is one source of truth.
- `run_daily` runs `tmdb → feeds (per_film=true) → link → synthesize` and is **fail-fast**:
  it `await`s each stage to completion, re-reads the run's terminal status, and aborts the
  chain on the first non-`succeeded` stage. Sequential `await` makes it structurally
  impossible for `synthesize` to start before `link` has fully finished.
- `run_hourly` runs the light feeds-only pass (`per_film=false`).
- Alerting is a **healthchecks.io deadman**: a best-effort ping (`/start` at the top, base
  URL on full success, `/fail` on any failure), configured per schedule via
  `HEALTHCHECK_DAILY_URL` / `HEALTHCHECK_HOURLY_URL` (unset → no-op).

Because there is no external poll window, slow Anthropic batches no longer fail the
pipeline; the deadman's grace period (daily ~2–3h, hourly ~30m) absorbs batch latency, so
batch mode (`LINK_USE_BATCHES` etc.) stays on for its ~50% cost saving. The startup
stale-run canceller (`main.py` lifespan) still bounds any run orphaned by a mid-run deploy.

## Considered alternatives

- **Keep GitHub Actions, widen the poll budget and gate `synthesize` on `link` success.**
  Rejected: it patches the symptom while keeping the fundamental coupling of a fixed CI
  budget to an unbounded batch wall-clock, and burns CI minutes idling on 30s polls for up
  to 2h.
- **Turn off batch mode for link/cluster** (`LINK_USE_BATCHES=false`) so stages run
  synchronously and finish fast. Rejected as the primary fix: it forfeits the batch cost
  saving; the in-process design keeps batch mode viable, and this remains available as a
  config lever if latency ever matters more than cost.
- **A separate one-off container / cron job hitting the API over HTTP.** Rejected: it
  reintroduces the trigger-and-poll indirection. Running in the app container gives direct
  DB access and lets the stages simply `await` in sequence.

## Consequences

- Ingestion scheduling now lives in Coolify (two scheduled tasks on the `upmovies-backend`
  resource) + healthchecks.io, not in the repo. The manual setup is recorded in NEU-741:
  cron `0 9 * * *` → `daily`, `0 * * * *` → `hourly`; two deadman checks whose URLs feed the
  `HEALTHCHECK_*` env vars.
- A scheduled task runs in the same container as `uvicorn`; a deploy mid-run restarts the
  container and the lifespan canceller marks the interrupted run `cancelled`. Accepted —
  same exposure the background-task model already had.
- `/admin/ingest/*` trigger + status endpoints are unchanged and remain available for manual
  runs; only the scheduler that calls them changed.
