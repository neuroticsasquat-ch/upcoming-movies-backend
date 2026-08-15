# Stable-prefix-first is the provider-neutral caching contract

**Status:** accepted — implemented in NEU-976

## Context

*backlotter: LLM Provider Gateway* exists to make model selection a configuration change
rather than a code change. Prompt caching was the part of that expected to be hard, because
it was the one piece of vendor knowledge every call site carried: before NEU-976 all four
builders emitted Anthropic wire format — a `system` list of content blocks plus a `messages`
list — and `link` had until recently marked its roster block with `cached_system_block(...)`.
By the time the DTO landed the roster path was already deleted (NEU-1004), so all four stages
were emitting plain, uncached system blocks and the helper survived only in
`scripts/propose_validation_labels.py`. The vendor shape, however, was still in every builder.

The two caching mechanisms in play have no shared vocabulary:

- **Anthropic caches explicitly.** The caller places a `cache_control` breakpoint and
  everything up to it is cacheable. Nothing is cached without the marker.
- **DeepInfra and DeepSeek cache automatically.** No marker exists; the provider matches the
  longest byte-identical prefix of the request against what it already holds.

There is no portable `cache_control`. A reader coming to `llm/types.py` expecting an
abstraction over cache blocks — the obvious shape, given Anthropic's API has one — will not
find one, and deserves to know that was deliberate.

The reconciliation is that the two mechanisms are not competing abstractions but two
realizations of a single requirement: **stable content first, byte-identical across calls.**
Anthropic needs it because a breakpoint placed after varying content caches nothing reusable;
automatic providers need it because their match runs from byte zero. Satisfy the invariant and
both engage — Anthropic by annotating, the others by inferring.

The call sites were already honouring it without naming it: every one of the four put its
fixed instruction text in the system position and its per-run payload in the user position —
`link` most visibly, since back when the prefix was the roster it was the daily-changing
`as_of_date` payload that sat in `messages`. The invariant existed and was unstated, which is
exactly the thing worth writing into a type.

## Decision

The neutral request DTO expresses caching as **prefix position and byte-stability**, not as a
cache-block abstraction:

```python
Prompt(stable_prefix: str, user: str, prefill: str | None = None, max_tokens: int = 4096)
```

`stable_prefix` is the builder's promise that the content does not vary call to call. Every
adapter is obligated to serialize it **first and unmodified**. Content that varies belongs in
`user`, or the promise is broken and no provider's cache will match. No builder names
`cache_control`, and `cached_system_block` is gone.

Each adapter then realizes the same contract natively:

- **Anthropic annotates.** `_to_wire` (`llm/client.py`) makes the prefix the one leading
  system block and marks it `cache_control: {"type": "ephemeral"}` — **unconditionally**.
  Deciding *whether* to mark would mean estimating token counts against a per-model floor
  (Haiku 4.5 = 4096, Sonnet 4.6 = 2048), which is precisely the vendor knowledge the DTO
  removes from the builders. Below the floor the marker silently no-ops, so marking always is
  free when it does nothing and correct when a prefix grows past it.
- **OpenAI-compatible adapters infer.** They emit the prefix as the leading system message,
  unchanged, and let automatic prefix caching engage on its own.

The shipped DTO differs from spec §5.1 in two places, both deliberate. `prefill` was added
because `summarize` seeds the assistant turn to force a JSON continuation, and an adapter whose
provider cannot prefill must raise rather than silently drop it. `json_object` was *not* added:
Anthropic ignores it and no OpenAI-compatible adapter exists yet, so a `response_format` lever
with nothing behind it would be an untested field the builders would have to reason about.
It lands with the adapter that consumes it (NEU-979).

**Cached-token reporting is a hard provider-selection criterion.** A provider that does not
report cached-token counts cannot be evaluated, because this project's own standard is that
cost must be measurable per model per stage or the switch is unfalsifiable. That disqualifies
a provider outright, whatever its headline price. Reporting must be verified empirically per
provider rather than assumed from documentation — field names vary, and DeepSeek has
historically used `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` rather than
`prompt_tokens_details.cached_tokens`. Counts, not booleans: `Usage` keeps
`cache_read_input_tokens` and `cache_creation_input_tokens`, and "cache hit" stays a derived
predicate.

## Considered alternatives

- **Keep Anthropic content blocks as the internal lingua franca and strip `cache_control` on
  the OpenAI path.** Rejected, and this is the alternative that matters. It reduces caching to
  *hoping* automatic prefix matching engages, with nothing in the request stating the
  invariant it depends on and nothing failing if a builder violates it. That is exactly the
  "changes the cost profile substantially and unpredictably" failure this project exists to
  prevent, except unobservable. It also leaves the vendor in all four builders, which was the
  coupling being removed.
- **Expose a portable cache-block / breakpoint abstraction.** Rejected: it models the
  mechanism only one provider has. On an automatic provider a breakpoint is either a no-op or
  a lie, and offering one invites builders to place markers that mean nothing on three of four
  candidate providers.
- **A `cache: bool` (or `cacheable_system`) flag per request.** Rejected: it names an outcome
  rather than the invariant that produces it. A builder can set `cache=True` and still put a
  timestamp in the prefix, and nothing catches it. Position-and-stability is a contract an
  adapter can honour mechanically; a boolean is a wish. Whether caching actually engages is
  also not the builder's to decide — it is a fact about the provider's minimum cacheable
  length.
- **Let each adapter infer the stable portion from the message list.** Rejected: an adapter
  cannot tell which bytes are stable across calls it has not seen. Only the builder knows,
  which is why the promise belongs in the type the builder constructs.

## Consequences

- Anthropic's wire format appears in exactly one module (`llm/client.py`). Each of the four
  stage builders now returns a `Prompt` carrying its own instruction constant as
  `stable_prefix` and its JSON payload as `user`, and names no vendor concept.
- The Anthropic path reproduces the previous request shape, verified at the adapter by
  `test_the_stable_prefix_becomes_the_leading_cached_system_block`
  (`tests/unit/llm/test_client.py`). The seven scattered `"cache_control" not in …`
  assertions across builder and pipeline tests collapsed into that one wire test — asserting
  on the mechanism now happens only at the layer that owns it.
- **The contract has landed inert, and that is an accepted outcome, not a regression.**
  *backlotter: Entity-Linking Candidate Retrieval* replaced `link`'s ~50k-token roster prefix
  with retrieval instructions plus per-story candidates (NEU-1004), leaving
  `_RETRIEVAL_INSTRUCTIONS` at roughly 1.5k tokens — under Haiku 4.5's 4096-token floor, and
  `link` runs on Haiku 4.5 (`config.py:32`). `cluster`, `summarize` and `source_judge` all sat
  below their floors already, so **backlotter does no prompt caching anywhere today** and all
  four `cache_control` markers no-op. ADR-0010 records the downstream consequence: cache
  read/write ratio alerting is not built, because the ratio is now undefined rather than
  merely healthy. Keep the contract regardless. A future reader
  finding an unexercised abstraction should know it was expected rather than abandoned — it is
  the right interface, it costs nothing while inert, and it engages by itself if a prefix ever
  grows back past a floor.
- Sequencing follows from that: the contract had to be built *after* Candidate Retrieval, not
  before, or it would have been built twice — once against the roster prefix and once against
  the prefix that replaces it.
- The measurability rule cuts down candidate providers before latency or quality is measured.
  It is the same standard ADR-0005 applied when it deleted the batch path: cache behaviour
  that cannot be observed per stage is what let batch mode waste ~74% of a $13.57 day
  unnoticed.
- The remaining gateway-shape decisions — the adapter/`Gateway` split, why not LiteLLM, and
  the shared retry policy — are recorded separately in ADR-0007 (NEU-982), which applies this
  same "measurable or it doesn't count" standard to pricing.
