# Provider response fixtures

One `chat/completions` response body per OpenAI-compatible provider, used by
`tests/unit/llm/test_openai_compat.py` to pin the `usage` → `Usage` mapping (NEU-979).

## Provenance — read before trusting these

These are **response shapes taken from each provider's published API documentation**, not
bodies captured from a live call. No `DEEPINFRA_API_KEY` / `DEEPSEEK_API_KEY` was available
when NEU-979 was implemented, so the empirical capability verification the ticket asks for —
confirming the cached-token fields are actually *populated*, that `response_format` is
genuinely honoured rather than merely accepted, and the observed JSON compliance rate — has
**not** been performed. The adapter is written against both documented shapes; the mapping is
correct for the shapes below, and the shapes themselves are still unverified.

Replace each file with a real captured body once credentials exist, and re-run the suite. If a
field name here turns out to be wrong, the test that reads it fails — which is the point of
keeping the mapping pinned to a whole recorded body rather than to a hand-written dict.

## What each one exercises

| File | Shape |
|---|---|
| `deepseek_chat_completion.json` | `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` — the disjoint pair, alongside the OpenAI-style `prompt_tokens_details.cached_tokens` |
| `deepinfra_chat_completion.json` | `prompt_tokens_details.cached_tokens` only, with `prompt_tokens` as an inclusive total |

The two differ in more than field names: DeepSeek hands over the uncached count directly,
while DeepInfra's `prompt_tokens` *includes* the cached tokens and must have them subtracted
before it can be priced (see `Rates` in `llm/pricing.py`).
