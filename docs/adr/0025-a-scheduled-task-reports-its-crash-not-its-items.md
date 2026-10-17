# A scheduled task reports its crash to the error tracker, not its failed items

**Status:** accepted 2026-10-17 — implemented in NEU-1623 (neuroticsasquat.ch Systems Strategy, M4).
**Supersedes:** ADR-0010 in part (its deferral of "wire Sentry into `pipeline_run`").
**Relates to:** NEU-1590 (the instrumentation contract, PR #418); the ops repo's
`docs/specs/neuroticsasquat-ch-systems-strategy-project-spec.md` §Instrumentation contract.

## Context

The Sentry SDK (pointed at Bugsink) was initialised only in the FastAPI app. The Coolify scheduled
tasks — `python -m upmovies.pipeline_run <mode>`, a separate process — initialised nothing, so a
run that crashed reached the error tracker never. It surfaced through the Uptime Kuma deadman and,
when the task is wrapped in `opentelemetry-instrument`, its span; neither one opens an issue with
a stack trace, and neither alerts Discord `#alerts` on a regression.

ADR-0010 listed "wire Sentry into `pipeline_run` and `capture_message` on breach" as *not
rejected on merit* and deferred it as out of scope. NEU-1590 aligned the API with the
instrumentation contract and left that deferral in place.

The cost the deferral was protecting against is real. Every pipeline catches a per-item failure,
`log.exception`s it, and carries on: a feed that times out, a film TMDB will not return, a story
the model mis-answers. The SDK's default logging integration turns each of those into an event,
so naively initialising it in `pipeline_run` would put a thin day's worth of items into Discord.

## Decision

A scheduled task reports **crashes, and only crashes** — one event per run:

- `main` calls `init_sentry()` with the same release and environment resolution as the API, so a
  task's event and the API's name the same build.
- The task's logging integration is `LoggingIntegration(event_level=None)`: log records stay
  breadcrumbs on whatever event does fire, and never become events themselves. That is the
  answer to the alert-volume concern above.
- A stage that crashes is caught so its run row can be finalized `failed`, and so reaches no
  SDK hook. `_finalize_failed` captures it alongside its existing `record_handled_exception`.
- An exception that escapes `main` — a guard refusing the configuration, a crash in the
  stale-run cleanup or the run insert — is captured there too.
- Every event is tagged `pipeline_mode=<mode>`, and the SDK is flushed before the process exits.
- `python -m upmovies.pipeline_run raise-test-error` raises `PipelineTestError`, the scheduled-task
  twin of the API's `POST /admin/debug/raise-test-error`, so the runbook can prove the path end to
  end.

`_finalize_failed` is shared with the `/admin/ingest/*` trigger endpoints, which changes nothing
there: the API keeps its default logging integration, so each stage's `log.exception("… crashed")`
already reported the crash, and the new capture of the same exception is dropped by the SDK's
dedupe integration.

A stage that ends `failed` **without** a crash — a **total stage failure**, a retrieval **hard
breach**, a sweep phase that aborted — is not an event. It was a decision the code made
about thin data, not a defect in the code.

## Considered alternatives

- **`capture_message` on every health-signal breach** (ADR-0010's other half). Rejected:
  breaches are threshold conditions SigNoz and Kuma already watch, and sending them to the error
  tracker mixes "the code crashed" with "the data looks thin" in the one channel that is meant to
  say only the former.
- **Initialise the SDK with its defaults.** Rejected for the per-item volume described above.
- **Leave it deferred.** Rejected: the deadman says *that* a task failed but not *where*, and the
  instrumentation contract asks every codebase to report its crashes to Bugsink.

## Consequences

- ADR-0010's deferral is superseded on this one point; the rest of ADR-0010 stands.
- A crashed scheduled task now opens (or regresses) a Bugsink issue and alerts `#alerts`, in
  addition to the Kuma deadman going down. The two are expected to fire together.
- A per-item failure is visible only in the logs and as a breadcrumb. If one class of item
  failure ever needs an alert of its own, it gets an explicit `capture_*` call and its own ADR,
  not a lower `event_level`.
