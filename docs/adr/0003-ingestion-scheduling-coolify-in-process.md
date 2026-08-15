# Run ingestion as in-process Coolify scheduled tasks, not HTTP-polled GitHub Actions

**Status:** accepted — batch-mode reasoning amended 2026-08-05, see ADR-0005

> **Amendment (2026-08-05).** This ADR's scheduling decision stands unchanged. Its
> **batch-mode** reasoning does not, and was self-contradictory as written: the Decision
> section concluded batch mode "stays on for its ~50% cost saving", while Considered
> alternatives listed turning it off as available "if latency ever matters more than cost".
>
> Both readings are now obsolete. Measurement showed the 50% discount was more than eaten by
> cache thrash — batch runs outlive the 5-minute ephemeral cache TTL, giving read/write
> ratios of 0.04–3.04 against 12–18x in standard mode. Production has run
> `*_USE_BATCHES=false` since. The batch path is being deleted outright; see
> [ADR-0005](0005-remove-message-batches-path.md).
>
> Read the two paragraphs below that mention batch mode as historical context for why the
> scheduler changed, not as current guidance.

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
batch mode (`LINK_USE_BATCHES` etc.) stays on for its ~50% cost saving. The stale-run
canceller still bounds any orphaned run, but no longer only on deploy and no longer on
`started_at`: it runs from the scheduled-task entrypoint as well as the app's lifespan, and
expires a run on `COALESCE(last_progress_at, started_at)` — a **heartbeat**, so a live
multi-hour sweep is never mistaken for one orphaned by a crash (NEU-1117).

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

- Ingestion scheduling now lives in Coolify (scheduled tasks on the `upmovies-backend`
  resource) + healthchecks.io, not in the repo. The manual setup is recorded in NEU-741:
  cron `0 9 * * *` → `daily`, `0 * * * *` → `hourly`; deadman checks whose URLs feed the
  `HEALTHCHECK_*` env vars.
- **A third slot, `0 7 * * *` → `sweep`** (NEU-1079), with its own `HEALTHCHECK_SWEEP_URL`
  check. Two hours ahead of `daily` so films it admits are in the retrieval index for that
  day's link pass, and outside the chain on purpose: `run_daily` is fail-fast, so a TMDB
  hiccup partway through a ~45-minute sweep would take feeds, link and synthesize down with
  it (ADR-0013, project spec §6.1). Its own deadman for the same reason — the daily check
  says nothing about a sweep that stopped running.
- A scheduled task runs in the same container as `uvicorn`; a deploy mid-run restarts the
  container and the lifespan canceller marks the interrupted run `cancelled`. Accepted —
  same exposure the background-task model already had.
- `/admin/ingest/*` trigger + status endpoints are unchanged and remain available for manual
  runs; only the scheduler that calls them changed.
