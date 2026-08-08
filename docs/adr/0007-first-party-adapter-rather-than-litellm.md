# The gateway is a first-party adapter, not LiteLLM

**Status:** accepted — *backlotter: LLM Provider Gateway* (M4), implemented across NEU-978 …
NEU-981

## Context

LiteLLM is the obvious answer to "call several LLM providers behind one interface." It is
the default choice for this problem, it is widely used, and it would have covered the three
providers in scope on day one. A reader arriving at `src/upmovies/llm/` and finding roughly
1,100 lines of hand-written adapter, registry, retry loop and pricing table deserves to know
that the library was considered and declined, and on what grounds — otherwise the honest
reading is that
nobody looked.

The starting position mattered more than it usually does. Three things were already true before
any of this work began:

- **The seam existed.** A `Completer` Protocol was already declared — twice, in fact, once in
  `link/linker.py` and once in `synthesize/summarizer.py`, having drifted into a copy per stage.
  The four call sites were already written against an interface rather than against a vendor
  client. The job was *adding a second implementation of an interface that already had one*, not
  introducing an abstraction.
- **The transport existed.** `httpx` was already a dependency and already OTel-instrumented
  (`pyproject.toml:14,24`). Every request the OpenAI-compatible adapter makes shows up in the
  same traces as every other HTTP call the service makes, for free.
- **The wire format converges.** Both candidate providers — DeepInfra and DeepSeek — speak
  OpenAI-compatible `/chat/completions`. The surface actually used is one POST with four fields
  (`model`, `messages`, `max_tokens`, and optionally `response_format`). Two providers on one
  format is a few hundred owned lines, tested with the respx patterns the repo already uses
  everywhere else.

So the trade was not "write an abstraction or adopt one." It was "write the second
implementation of an existing abstraction, or take a dependency that brings its own."

## Decision

**Hand-roll the adapter layer.** `src/upmovies/llm/` owns its transport (`anthropic.py` over the
vendor SDK, `openai_compat.py` over `httpx`), its endpoint constants (`registry.py`), its retry
loop (`retry.py`), its per-stage resolution (`gateway.py`) and its cost table (`pricing.py`). No
routing library.

### The deciding factor: cost-table semantics

Everything above is a preference. This is the part that decided it.

`pricing.py` raises `KeyError` on an unknown `(provider, model)` pair, deliberately:

```python
def rates_for(provider: str, model: str) -> Rates:
    """..."""
    return _RATES[provider, model]
```

That is not an oversight to be smoothed over — it is the single most load-bearing line in the
module. This whole project exists because a cost profile shifted unpredictably and nobody
noticed (ADR-0005: batch mode wasted roughly 74% of a $13.57 day on cache writes that expired
before anything read them). The guarantee that replaced that failure is: **a stage routed at a
pair nobody has priced fails loudly, immediately, rather than reporting a number that is wrong.**

LiteLLM's bundled cost tables invert exactly that. An unknown model falls back to zero or to a
stale rate, which is the *silent mispricing* branch this project spent a milestone eliminating.
Adopting the library means one of two things:

- trust its tables — and give up the property that a mispriced stage is impossible rather than
  merely unlikely, in the one project whose premise is that unmeasurable cost is unfalsifiable
  cost; or
- keep our own tables anyway — and then the library is saving us the two-provider HTTP call it
  was adopted for, while we still maintain the part that was actually hard.

Neither is worth a dependency. And the cost table is where the real work turned out to be:
`Rates` carries four required fields with no defaults because Anthropic's `1.25` cache-write
premium is wrong by construction on an automatic-caching provider, and `_usage_from` has to
subtract cached tokens out of an inclusive `prompt_tokens` total before pricing, or every cached
token is charged twice. That reasoning is provider-specific, empirically verified, and would
have had to be written whichever way this went.

## Considered alternatives

- **Adopt LiteLLM wholesale.** Rejected on the cost-table semantics above, and on three further
  counts consistent with it:
  - *Dependency surface.* A large transitive tree to serve one POST with four fields.
  - *Layered semantics.* LiteLLM ships its own retry, fallback and router behaviour. Ours are
    not decorative: `retry.py` is shared by both adapters *specifically* so a provider
    comparison is not partly measuring two different retry loops, and the gateway has no
    cross-provider fallback at all (below). Running our policy on top of the library's means
    two retry regimes composed, with the observed attempt count belonging to neither.
  - *Behaviour drift across releases.* A routing library's job is to keep up with many
    providers, so its behaviour moves. Evaluation runs are comparisons over time; a silent
    change to how a call is retried or a token counted is a change to the measurement
    instrument.
- **LiteLLM for the OpenAI-compatible providers, the Anthropic SDK natively.** Rejected, and
  the one worth naming, because it initially looks like the pragmatic compromise. It is the
  worst of the three: two abstraction layers instead of one, and a `Completer` shim still
  required on top to unify them — which is to say, all of the library's cost and none of its
  "one interface" benefit. The shim is the part we were supposedly avoiding writing, and it
  survives this option intact.
- **Trust LiteLLM's cost tables and delete `pricing.py`.** Rejected outright: it is the
  silent-mispricing branch stated as a plan. See above.

## Folded-in decisions

These are smaller than an ADR each but belong on the record, and all four follow from the same
"measurable or it doesn't count" standard.

**`(provider, model)` rate keying, with required per-provider cache multipliers.** Keyed on the
model id alone, `_RATES` collides the first day two hosts serve the same open weights — which is
the project's entire premise. DeepInfra and DeepSeek both serve DeepSeek V4, at different
prices. `cache_write_mult` and `cache_read_mult` moved into `Rates` as *required* fields for the
same reason a default would be wrong rather than merely lax: an automatic-caching provider has
no explicit write to charge for, so a defaulted entry would hand a new provider Anthropic's
1.25x write premium silently. (NEU-978.)

**Boot-time validation of rates and credentials.** `rates_for`'s `KeyError` is the right
behaviour and the wrong timing: the stages commit per item, so an unpriced pair surfaces partway
through a nightly publish with earlier work already committed. `validate_stage_configuration`
runs at lifespan startup and asserts every configured stage resolves to `Rates` *and* has a
credential, reporting all faults together. This is what makes optional `DEEPINFRA_API_KEY` /
`DEEPSEEK_API_KEY` safe — the same class of guard as `TMDB_API_KEY` being required for the app to
boot at all. (NEU-981.)

**The `Gateway` resolver, and why `model` stays an explicit parameter.** `run_link_stage` opened
one `AnthropicClient` and passed it down, and that one instance backed *three* call sites — link,
cluster and source_judge all run inside `run_link_ingest`. Per-stage providers cannot be threaded
through one client instance; that structural fact, not a taste for indirection, is why
`gateway.py` exists. Clients pool by provider rather than by stage, so the default configuration
(four stages on Anthropic) stays one connection pool.

`model`, however, is deliberately *not* resolved there. Folding it in —
`complete_with_usage(..., stage="cluster")`, gateway reads the model from settings — looks like
the deeper module and is the tempting shape. It breaks `scripts/eval_cluster_diff.py`, whose
entire A/B mechanism is `--model claude-sonnet-4-6` then `--model claude-haiku-4-5` against a
fixed corpus. M5 extends those harnesses rather than rewriting them, and their corpus-state
fidelity guarantees (read-only runs, `story_id` alignment, one `--reset` up front) are
stage-specific reasoning nobody wants to rediscover three times. So the gateway varies the
*provider* axis — `Gateway(settings, overrides={"cluster": "deepinfra"})`, validated on both
halves so a typo is refused rather than quietly ignored — and the pipelines keep varying the
*model* axis. Those two knobs are how an eval sweep reaches DeepInfra and DeepSeek at all,
given there is no fallback to stumble onto them by accident. (NEU-980.)

**One retry policy, shared by both adapters rather than configured to matching numbers.** This
is the folded-in decision most easily mistaken for an implementation detail. Two adapters set to
similar-looking values still retry differently — different status sets, different backoff curves,
different readings of `Retry-After` — and once they do, every latency and coverage comparison
between two providers is partly measuring the retry loops and reporting it as provider quality.
`retry.py` therefore holds the loop, the classifier verdict type and the `Retry-After` handling,
and each adapter contributes only the vendor-specific step of getting a status and headers off
its own failure type.

Two specifics are worth the record. `httpx.AsyncHTTPTransport(retries=N)` does **not** do this
job — it retries connection *establishment* only, and 429/5xx is most of what needs retrying.
And under-retrying does not surface as an error: `_link_stage_sequential` catches per chunk,
records `failed_delta` and continues, so a provider that gives up one attempt early loses a
silent chunk of 15 stories, which an eval run reads as "this provider links fewer stories". That
is the measurement-integrity argument in its sharpest form, and it is why the policy is shared
rather than merely aligned. (NEU-979.)

**No cross-provider fallback.** The gateway never silently fails over to Anthropic. A stage
configured for a provider with no credential raises `MissingCredentialError`; it does not
resolve to something that works. Under a capability-only exit criterion this buys nothing —
production is Anthropic for all four stages regardless — and during evaluation it is actively
destructive: it would attribute one provider's latency, cost and coverage to another, which is
the one failure mode the numbers afterwards cannot reveal. A failing provider means a failing
chunk, handled by the per-chunk isolation that already exists.

## The honest limit

**At ten providers, this stops paying.** The arithmetic here rests on *two* providers sharing
*one* wire format. Each additional format is a new adapter, a new usage-block mapping, a new set
of retry classifications and a new column of the cost table — and the fixed cost a routing
library amortizes is precisely that per-provider tail. If backlotter ever routes across many
providers rather than a handful, revisit this: the decision is a function of N, and N is
currently 3 (one of which is reached through its own SDK).

The narrower version of the same limit already showed up. `anthropic.py` still runs on the
vendor SDK rather than on `httpx`, because rewriting a working, tested Anthropic path bought
nothing. So the layer is not uniformly first-party-over-`httpx`; it is a Protocol with two
implementations that happen to reach their providers differently, which is what the Protocol was
for.

## The exit criterion, and its accepted risk

Worth stating plainly, because everything above reads as preparation for a migration that is not
going to happen on this project's watch:

**Production stays on Anthropic for all four stages.** DeepInfra and DeepSeek are proven in
offline evaluation only. Nothing migrates. Cost is explicitly not the justification — backlotter's
entire LLM spend is about $0.31/day — roughly $9/month — which is the ceiling on what any
migration could save, whatever the provider charges. The motivation is optionality and
instrumentation ahead of the undated-film expansion, which multiplies volume across every stage.

**The accepted risk: an adapter with no production canary is unexercised code, and unexercised
code rots.** `openai_compat.py` will be exercised by its unit tests and by eval runs, and by
nothing else. Its usage-block mapping in particular is calibrated against field shapes verified
empirically once (`scripts/verify_provider_capabilities.py`) and pinned into fixtures; providers
change their reporting, and a silent change would be caught by nothing until an eval run some
months later produced a cost number nobody could reconcile.

This is recorded deliberately rather than mitigated. The mitigation on offer — route one
low-stakes stage in production as a canary — is a migration, and migrating is the thing the exit
criterion says we are not doing. Revisit if the expansion changes the economics enough to make a
real migration worth costing out.

## Consequences

- `src/upmovies/llm/` is a flat package matching the `ingest/tmdb/` idiom, and everything in it
  is ours to maintain: `types.py`, `anthropic.py`, `openai_compat.py`, `registry.py`, `retry.py`,
  `gateway.py`, `pricing.py`. Nothing under `llm/` imports `sqlalchemy`, `upmovies.db` or
  `upmovies.ingest`, enforced by a test (`tests/unit/llm/test_no_db_imports.py`) — that
  separation is what lets the whole layer be tested against respx without a database.
- Adding a provider is a known, bounded edit rather than a config change: a `registry.py`
  constant plus its `PROVIDERS` and `_OPENAI_COMPAT_BASE_URLS` entries, `_RATES` entries verified
  against the published pricing page, a credential on `Settings` *and* in `credential_for`'s key
  map, and the config `Literal` widened. Five places, all of them small — but all five, since a
  provider missing from `credential_for` raises `KeyError` at boot rather than the clean
  "credential unset" failure the message promises. The `KeyError` discipline in `rates_for`,
  `base_url_for` and `credential_for` is what makes a half-finished addition fail at boot rather
  than mid-run.
- Rates are a maintenance obligation with no automation behind it. Each provider's block carries
  the date and URL it was verified against (`pricing.py:53-103`), and DeepSeek has announced a
  price increase; re-verify before trusting the dollar figures. Raw token counts remain the
  recorded source of truth precisely because they do not go stale.
- This ADR applies to pricing the same standard
  [ADR-0006](0006-stable-prefix-first-caching-contract.md) applied to caching: a provider whose
  cached-token counts cannot be observed cannot be
  evaluated, whatever its headline price. ADR-0006 is the neutral request contract the adapters
  realize; this one is the shape of the adapters that realize it.
- Both ADRs inherit their standard from [ADR-0005](0005-remove-message-batches-path.md), which
  deleted a path whose cost behaviour was invisible per stage. "Silent mispricing is worse than a
  crash" is the same sentence in a third context.
