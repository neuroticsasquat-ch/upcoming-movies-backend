# Retrieval health replaces the cache read/write ratio guardrail

**Status:** accepted — implementation tracked in *backlotter: Entity-Linking Candidate Retrieval* (M2, M4)

## Context

The *backlotter: Entity-Linking Candidate Retrieval* project brief mandates one specific piece
of instrumentation: *"Add monitoring on the cache read/write ratio, alerting below ~5."* It was
moved onto this project from *backlotter: LLM Provider Gateway* on 2026-08-05 precisely so the
alerting would live where the risk lives. This ADR records why it is nonetheless not being
built, so a dropped requirement is traceable rather than merely absent.

The alert guards a real historical failure. Batch-mode runs inverted the ratio badly — 0.04 on
2026-07-13, 0.12 on 2026-07-15, wasting ~$10.02 of a $13.57 day on cache writes that expired
before being read. Standard mode currently holds 12–18x only because a run completes inside the
5-minute ephemeral cache TTL, and the brief correctly identifies the exposure: more titles →
more stories → more calls → a longer run → the cache drops mid-run.

Two facts remove the risk rather than mitigate it.

**`link` is the only stage that caches.** `llm/client.py:19-21` records the per-stage position:
`link` caches iff `instructions + roster` clears Haiku 4.5's 4096-token floor; `cluster` and
`summarize` use plain blocks and do not cache; `source_judge` uses none. Deleting the roster
prefix leaves a ~1.5k-token instruction block, below every floor. **After cutover, backlotter
does no prompt caching anywhere**, and the ratio is undefined rather than merely healthy.

**The risk window never opens.** The TTL exposure arrives with the catalog expansion, but
*backlotter: Undated Film Discovery* is blocked on this project. The expansion cannot land
before the prefix it would stress is deleted. An alert built now would guard an interval in
which its failure mode is unreachable, then measure a metric that no longer exists.

Meanwhile this design introduces a new failure that nothing currently watches. As the catalog
grows and titles collide, the zero-candidate rate drifts upward and stories are rejected with
no model ever seeing them (ADR-0009) — in a lossy stage, permanently. From the outside that is
indistinguishable from a quiet news day.

## Decision

Do not build cache read/write ratio alerting. Build **retrieval-health** alerting in its place:
zero-candidate rate, candidate-cap saturation rate, and mean candidates per story.

Delivery is two-tier, reusing what exists rather than adding infrastructure:

- **Hard breach** — a zero-candidate rate past threshold finalizes the run `failed`, which
  makes `run_daily` abort and ping the healthchecks.io deadman `/fail`. Subject to a
  minimum-denominator rule, mirroring `total_failure_error`'s existing refusal to let one bad
  item on an empty backlog fail the chain daily.
- **Soft signal** — drift below that threshold is recorded per run and surfaced on
  `/admin/runs` for inspection.

> **Amended 2026-08-11 (NEU-1088): the soft tier gains a threshold.** As delivered, "soft
> signal" meant *recorded and visible* — cap saturation had no threshold at all, and only the
> zero-candidate rate was ever compared against one. That gap did exactly what this ADR was
> written to prevent, one metric over: after the directors tranche, saturation went 0% → 7.9%
> in three days and nothing raised a hand. It was found by querying production by hand.
>
> The soft tier is now a **soft breach**: a saturation rate above
> `LINK_RETRIEVAL_SATURATION_WARN_RATE` sets `soft_breach` on the run's health row and names
> itself in the run's `detail` line. **It does not fail the run.** That is the doctrine this
> ADR already implies, stated outright — the hard tier catches collapse, the soft tier catches
> drift, and rising saturation is drift by definition: the signal that says *retune*, not
> *outage*. `run_daily` is fail-fast, so a hard tier on saturation would publish no summaries
> at all on a day when nothing was actually broken.
>
> Two-tier now means two *thresholds*, not one threshold and one dashboard.
>
> The same ticket tightened the hard tier's ceiling from 25% to 10%. The reasoning is the
> asymmetry this ADR's own framing depends on: the ceiling has to sit below what a mis-set T
> would produce or it cannot catch the failure named above, and that margin decays without
> anyone touching it, because zero-candidate rates fall as the catalog grows. See design spec
> §5.13.

## Considered alternatives

- **Build both, retiring the cache alert at cutover.** Rejected: an alert with a known expiry
  date, guarding a window in which its failure mode cannot occur.
- **Cache-ratio alerting only, as briefed.** Rejected: it would leave silent over-rejection —
  the failure this design actually creates — unmonitored on the far side of cutover.
- **Wire Sentry into `pipeline_run` and `capture_message` on breach.** Not rejected on merit.
  Sentry is initialized only in the FastAPI app (`main.py`), so the scheduled pipeline reports
  to nothing today, and closing that gap is worthwhile — but it is scope this project did not
  ask for. Left for *backlotter: Maintenance*.
- **Admin surface only, no automated alert.** Rejected: it relies on someone looking, and the
  failure mode is specifically one that looks like nothing happening.

## Consequences

- Prompt caching leaves the codebase entirely. The *LLM Provider Gateway* spec anticipated this
  and called it **"a success, not a failure"** — its provider-neutral caching contract is kept
  regardless, and is expected to be inert.
- Failing a run on a *rate* is a departure from `StageCounts`' deliberately narrow
  "produced nothing at all" rule. It is scoped to retrieval health and does not change
  `total_failure`, which continues to guard model availability independently (ADR-0009).
- If prompt caching ever returns — a provider with a lower floor, or a prefix that grows back —
  cache-ratio alerting must be reconsidered. The underlying data needs no new work:
  `ingest.run_llm_usage` already stores `cache_read_input_tokens` and
  `cache_creation_input_tokens` per `(run, stage)`.
