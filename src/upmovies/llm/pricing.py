"""Single source of truth for LLM cost math. Lifted out of the offline `measure_link_cost`
script (NEU-297) so the measurement harness and production telemetry (NEU-375) would price
identical dollars from the same constants; that script was retired with the roster path
(NEU-1004) and the constants stay here.

Rates are keyed on `(provider, model)`, not on the model id alone: two hosts serving the same
open weights price them differently, which is the premise of the gateway work (spec §7). Adding
a model means adding its `Rates` under the provider that serves it — `rates_for` raises
`KeyError` on an unknown pair rather than silently mispricing it."""

from dataclasses import dataclass

from upmovies.llm.types import Usage

# The batch path applies a flat 50% discount on the whole total. It is kept for historical
# `run_llm_usage` rows: the Message Batches path itself is gone (ADR-0005), but rows recorded
# under it must keep re-pricing at the discount they were actually charged at.
_BATCH_DISCOUNT = 0.50


@dataclass(frozen=True)
class Rates:
    """Per-million-token USD rates plus the provider's own cache economics.

    All four fields are required — **no defaults, deliberately**. Cache pricing does not
    generalize across providers: Anthropic charges an explicit write premium (1.25x base input
    for the 5-minute ephemeral cache) because there is an explicit write to charge for, while
    automatic-caching providers match the longest byte-identical prefix and charge no write
    premium at all. A default would hand a new provider Anthropic's cache economics silently,
    which is precisely the unpredictable cost-profile shift this work exists to prevent.

    `cache_write_mult` and `cache_read_mult` are multipliers on `input_per_mtok`, so an entry
    stays readable against a pricing page that quotes cached input in dollars.

    **The multipliers assume `Usage`'s four counts are disjoint** — `input_tokens` is
    uncached input only, and cached tokens are counted *solely* under
    `cache_read_input_tokens`. That is how Anthropic reports, but OpenAI-compatible providers
    generally report `prompt_tokens` as a total that already *includes* the cached tokens
    broken out beside it. An adapter that maps such a total straight onto `input_tokens`
    charges every cached token twice — once at base and once at the cache-read rate. Subtract
    the cached count first (DeepSeek's `prompt_cache_miss_tokens` is already the disjoint
    figure); the obligation is the adapter's, because `price` cannot detect the overlap.

    VERIFY against the provider's published pricing page before trusting the $ figures — raw
    token counts are the recorded source of truth."""

    input_per_mtok: float
    output_per_mtok: float
    cache_write_mult: float
    cache_read_mult: float


# --- Anthropic --------------------------------------------------------------------------
# Verified 2026-08-08 against https://platform.claude.com/docs/en/about-claude/pricing:
# Haiku 4.5 $1.00/$5.00, Sonnet 4.6 $3.00/$15.00; 5-minute cache write 1.25x base input,
# cache hit 0.10x. (The 1-hour cache write is 2x; nothing here uses the 1-hour TTL.)
HAIKU_4_5 = Rates(
    input_per_mtok=1.00,
    output_per_mtok=5.00,
    cache_write_mult=1.25,
    cache_read_mult=0.10,
)
SONNET_4_6 = Rates(
    input_per_mtok=3.00,
    output_per_mtok=15.00,
    cache_write_mult=1.25,
    cache_read_mult=0.10,
)

# --- DeepSeek (first-party) -------------------------------------------------------------
# Verified 2026-08-08 against https://api-docs.deepseek.com/quick_start/pricing. Caching is
# automatic, so there is no write to charge for: cache-miss input *is* base input, and
# `cache_write_mult` is 1.0. The published cache-hit price is quoted below each entry.
# DeepSeek notes a planned increase to its API pricing — re-verify before trusting these.
DEEPSEEK_V4_FLASH = Rates(
    input_per_mtok=0.14,
    output_per_mtok=0.28,
    cache_write_mult=1.0,
    cache_read_mult=0.0028 / 0.14,  # $0.0028 cache-hit input against $0.14 base
)
DEEPSEEK_V4_PRO = Rates(
    input_per_mtok=0.435,
    output_per_mtok=0.87,
    cache_write_mult=1.0,
    cache_read_mult=0.003625 / 0.435,  # $0.003625 cache-hit input against $0.435 base
)

# --- DeepInfra --------------------------------------------------------------------------
# Verified 2026-08-08 against https://deepinfra.com/models/text-generation. Same weights as the
# DeepSeek entries above, different provider, different price — which is why `_RATES` is keyed
# on the pair. Automatic caching here too, hence no write premium.
DEEPINFRA_DEEPSEEK_V4_FLASH = Rates(
    input_per_mtok=0.09,
    output_per_mtok=0.18,
    cache_write_mult=1.0,
    cache_read_mult=0.018 / 0.09,  # $0.018 cached input against $0.09 base
)
DEEPINFRA_DEEPSEEK_V4_PRO = Rates(
    input_per_mtok=1.30,
    output_per_mtok=2.60,
    cache_write_mult=1.0,
    cache_read_mult=0.10 / 1.30,  # $0.10 cached input against $1.30 base
)


# (provider, model) -> rates. Add an entry when a stage starts using that pair (else
# `rates_for` raises). Model ids are the strings each provider's own API accepts, so they are
# not interchangeable between providers even where the weights are.
_RATES: dict[tuple[str, str], Rates] = {
    ("anthropic", "claude-haiku-4-5"): HAIKU_4_5,
    ("anthropic", "claude-sonnet-4-6"): SONNET_4_6,
    ("deepseek", "deepseek-v4-flash"): DEEPSEEK_V4_FLASH,
    ("deepseek", "deepseek-v4-pro"): DEEPSEEK_V4_PRO,
    ("deepinfra", "deepseek-ai/DeepSeek-V4-Flash"): DEEPINFRA_DEEPSEEK_V4_FLASH,
    ("deepinfra", "deepseek-ai/DeepSeek-V4-Pro"): DEEPINFRA_DEEPSEEK_V4_PRO,
}


def rates_for(provider: str, model: str) -> Rates:
    """Look up the per-mtok rates for a `(provider, model)` pair. Raises KeyError on an unknown
    pair — routing a stage at a new pair means adding its `Rates` to `_RATES`, and crashing is
    better than pricing it at whatever another provider charges for the same weights."""
    return _RATES[provider, model]


def price(usage: Usage, rates: Rates, *, batch: bool) -> float:
    """Dollar cost of `usage` at `rates`. Cache writes and reads are priced off base input by
    the entry's own multipliers; the batch path applies a flat 50% discount on the whole
    total (historical rows only — ADR-0005)."""
    base_in = rates.input_per_mtok / 1_000_000
    out = rates.output_per_mtok / 1_000_000
    cost = (
        usage.input_tokens * base_in
        + usage.cache_creation_input_tokens * base_in * rates.cache_write_mult
        + usage.cache_read_input_tokens * base_in * rates.cache_read_mult
        + usage.output_tokens * out
    )
    return cost * (_BATCH_DISCOUNT if batch else 1.0)
