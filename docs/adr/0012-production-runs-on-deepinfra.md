# Production runs on DeepInfra, not Anthropic

**Status:** accepted — *backlotter: LLM Provider Gateway*, NEU-1015. Reverses the
capability-only exit criterion this project was scoped under.

## Context

*backlotter: LLM Provider Gateway* was scoped deliberately narrowly: build the adapter, prove
both providers end-to-end in offline eval runs, and **leave production on Anthropic**. Nothing
migrates. That criterion is written into the project summary, into M5, and into spec §1.

It came with a named cost, recorded in M5's own description at the time:

> Accepted risk of the capability-only exit: an adapter with no production canary is unexercised
> code, and unexercised code rots.

Two facts move the balance far enough to reverse it.

**There are no users.** The app is live but its only reader is its author. The blast radius of a
bad night is one person's timeline, and everything the pipeline produces is re-derivable from
`news.story` — events can be deleted and the stage re-run, which is exactly what
`eval_cluster_diff.py --reset` already does.

**The instrumentation to evaluate live already landed, on purpose.** M2 put per-call telemetry
on the Anthropic path *before* any provider work, sequenced that way so the Anthropic baseline
distribution would already be recorded when the gateway arrived — "a provider comparison against
no baseline is not a comparison." It is recorded. `ingest.llm_call` holds per-call latency,
attempts, cached tokens, cost, `parse_ok` and now `truncated` for every Anthropic call the
pipeline has made.

So the live run is a measurement against a recorded baseline rather than a leap, and the offline
matrix M5 specifies — twelve cells, a new `source_judge` validator, four harnesses made
provider-aware — is the expensive way to answer a question production can answer for free.

## Decision

**Route all four stages to DeepInfra.** `link`, `summarize` and `source_judge` on
`deepseek-ai/DeepSeek-V4-Flash`; `cluster` on `deepseek-ai/DeepSeek-V4-Pro`.

M5's offline matrix is not run as specified. The per-stage validators stay where they are and
stay useful — they are how a stage that looks wrong in production gets diagnosed — but they
stop being a gate that must pass before anything ships.

`cluster` takes the Pro tier because it carries the highest quality bar. ADR-0004 established
that its single call does seven jobs, only one of which is similarity, and `CONTEXT.md`'s
over-merge failure mode is asymmetric: a swallowed beat disappears from the timeline entirely
rather than appearing as something visibly wrong. It is the stage where being wrong is quiet.

### Why DeepInfra alone, and not first-party DeepSeek

The pricing table argues the other way, and was verified on 2026-08-08: first-party DeepSeek
serves V4-Pro at $0.435/$0.87 per Mtok against DeepInfra's $1.30/$2.60 — the same weights, three
times cheaper, which is precisely the observation `_RATES` is keyed on `(provider, model)` to
capture.

It loses on operations anyway:

- **The price is moving.** DeepSeek has announced an increase, which `pricing.py` already flags
  in its own comment. A 3x gap that is about to change is not a number to plan against.
- **There is no automatic top-up.** An empty balance stops the nightly publish, silently, with
  no fallback — the gateway has no cross-provider failover by deliberate design (M4: silent
  failover "would contaminate evaluation results with calls attributed to the wrong provider").

One provider means one credential and one balance to watch, which for a single-operator service
outweighs a discount on a stage that costs ~$0.04/day.

### Cost is not the reason

It is worth stating plainly, because the change looks like a cost decision and is not. backlotter
runs ~$0.31/day in total — ~$113/year, which is the ceiling on what any migration could save
whatever the replacement charges — and `cluster` is ~$0.04/day of it, so the stage taking the
*more* expensive tier here is one costing ~$15/year to begin with. (Spec §1 carries a correction
worth repeating: an earlier draft put the total saving at "under $10/year", which was
year-for-month.)

The realized saving will also be smaller than the sticker ratio implies: both DeepInfra models
are reasoning models, reasoning tokens bill at the output rate, and the capability probe measured
~440 output tokens per call against a handful of visible ones. Output is the dominant term.

The reasons are exercising the adapter, learning the real latency distribution, and having the
provider axis be something the service actually does rather than something it could do.

## Consequences

**The reply budgets are now uncalibrated.** `link_batch_size: 20` was derived from a worst-case
*Haiku* reply of 1,183 tokens against `_MAX_TOKENS = 2048` — 58% of the ceiling. Reasoning tokens
sit inside `completion_tokens` and count against that same ceiling while being invisible in the
returned text, so the measurement does not transfer. `LINK_BATCH_SIZE` ships at **10** in
production as a hedge, and NEU-1014 added `ingest.llm_call.truncated` so the telemetry can say
whether it needs turning — a truncated unparseable reply and a malformed one look identical
otherwise, and their fixes are opposite.

**Two stages needed work the cutover did not anticipate, both found on the first production
run** (2026-08-08) and both recorded here rather than left to be rediscovered:

* `summarize` **could not run on DeepInfra at all.** Its prompt seeds the assistant turn, and the
  OpenAI chat-completions API has no slot for a prefix to continue, so every call was refused
  before it was made. Fixed under NEU-1016 by making the prefill a preference the caller declares
  rather than an absolute precondition — legitimate here because `parse_summary` reads a full
  envelope on its primary path, measured 10/10 against DeepInfra. Boot validation did not catch
  it: NEU-981 checks rates and credentials, not whether a provider can serve a stage's prompt
  *shape*. That is a real gap in the boot check, not a gap in the config.
* `source_judge` **never runs.** Google News is paused (ADR-0001), so ingestion is trade-feeds-only
  and no unknown domains reach the judge. Configuring it for DeepInfra is harmless and inert. It
  becomes live again the moment `NEWS_GOOGLE_ENABLED` is flipped — see NEU-984.

So the switch is really **two** exercised stages, `link` and `cluster`, plus one repaired and one
dormant. Both exercised stages showed non-zero cache reads on their first run, so DeepInfra's
prefix caching engaged.

**Rollback is env-only.** `docker-compose.prod.yml` reads all eight stage settings from the
environment with the DeepInfra values as fallbacks, and `ANTHROPIC_API_KEY` stays populated.
Reverting is eight Coolify variables and a restart — no redeploy, no key to go and find.

**`parse_ok` becomes an operational signal, not just an eval one.** The four stages keep their
four different parse-failure behaviours — unifying them is an explicit non-goal (spec §12) — so
what a bad model looks like differs per stage: `link` raises and takes its batch down, `cluster`
raises `ClusterParseError` per film, `summarize` recovers, `source_judge` warns.

**The M5 matrix is not the project's exit any more.** M5 is amended rather than deleted: the
harness gaps it identifies are real, and `validate_source_judge.py` (NEU-984) is still the one
stage with no validator at all.

## Considered alternatives

- **Run M5 as specified, then decide.** The thorough answer, and the one the project was
  designed around. Rejected on cost-of-information: twelve cells plus two new pieces of harness,
  to answer for one user a question that a week of live telemetry answers against a baseline
  that is already recorded.
- **Switch three stages and hold `cluster` on Sonnet 4.6.** Genuinely tempting, and the
  conservative read of the over-merge risk. Rejected because it leaves the highest-bar stage
  the only unexercised one, which is the half of the adapter most worth exercising; the Pro
  tier is the hedge instead.
- **Keep first-party DeepSeek in the mix for the Pro tier.** Rejected on top-up and price
  volatility above, not on price or capability — it passed the same verification DeepInfra did.
