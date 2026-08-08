# Provider response fixtures

One `chat/completions` response body per OpenAI-compatible provider, used by
`tests/unit/llm/test_openai_compat.py` to pin the `usage` → `Usage` mapping (NEU-979).

## Provenance

**Captured verbatim from the live endpoints on 2026-08-08**, by
`python scripts/verify_provider_capabilities.py --capture-fixtures`. Nothing here is
hand-written, and nothing is edited after capture — that is the point. The field most likely to
be wrong in a mapping is a *field name*, and a hand-written dict can only ever agree with
whatever its author assumed.

Both are the **warm** call of a two-call probe: one large byte-identical prefix sent twice, with
different payloads, so a hit is evidence of *prefix* caching rather than whole-response caching.
The prefix is synthetic rather than a stage's, because no current stage prefix clears a cache
floor — spec §3 anticipated exactly that, and it is now observed rather than predicted.

Re-run the script with `--capture-fixtures` when a provider changes its API; if a field name
moves, the test that reads it fails.

## What each one exercises

| File | Shape |
|---|---|
| `deepseek_chat_completion.json` | `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` — the disjoint pair — alongside the OpenAI-style `prompt_tokens_details.cached_tokens`, plus `completion_tokens_details.reasoning_tokens` |
| `deepinfra_chat_completion.json` | `prompt_tokens_details.cached_tokens` only, with `prompt_tokens` as an inclusive total, and a `cache_write_tokens: null` |

The two differ in more than field names: DeepSeek hands over the uncached count directly
(4608 + 119 = 4727), while DeepInfra's `prompt_tokens` *includes* the cached tokens and must
have them subtracted (4648 − 4352 = 296) before it can be priced. See `Rates` in
`llm/pricing.py` for why disjointness is load-bearing.

## Capability verification — results

The ticket makes empirical verification a precondition, not a nicety: a provider that cannot
report cached tokens cannot be evaluated at all (design §10). Run against
`deepseek-v4-flash` and `deepseek-ai/DeepSeek-V4-Flash` on 2026-08-08.

| | DeepSeek | DeepInfra |
|---|---|---|
| **1. Cached tokens reported and populated** | **Pass** — 4608 cached on the warm call, in both field shapes, agreeing | **Pass** — 4352 cached on the warm call |
| **2. `response_format` honoured** | **Yes, but gated** — HTTP 400 `"Prompt must contain the word 'json' in some form"` when the prompt never says "json"; honoured once it does, and does *not* force an object (returned a top-level array) | **Yes, unconditionally** — prose without the field, JSON with it, no prompt requirement; forces a top-level **object** |
| **3. JSON compliance, real `source_judge` prompt, 10 runs** | 10/10 without `response_format`; 10/10 with | 10/10 without `response_format`; **0-2/10 with**, across three runs |

**Neither provider needs reporting back as un-evaluable** — criterion 1 passes on both, so the
gateway's cost comparison is falsifiable on both.

The `response_format` results are why no builder sets `json_object`. Turning it on globally
would 400 every DeepSeek call at a stage whose prompt never says "json", and would break the two
array-shaped stages on DeepInfra, which answers for one domain instead of five (or emits a
JSON-Schema stub) when forced into an object. Both providers already comply 10/10 with the field
left alone. Details in `llm/types.Prompt`.

Also observed, and relevant to NEU-985's latency numbers: these are reasoning models. A DeepSeek
reply of 40 completion tokens was 38 reasoning tokens, and the compliance probe averaged ~440
output tokens per call against a handful of visible ones. Reasoning tokens bill at the output
rate and are already inside `completion_tokens`, so `Usage.output_tokens` needs no adjustment —
but a cost estimate built from visible output length would be wrong by an order of magnitude.

DeepInfra's prefix cache also did **not** engage on one run whose identical prefix had been sent
minutes earlier, then did on the next. Automatic caching is best-effort; do not read a single
cold run as "this provider does not cache".
