"""Single source of truth for Claude API cost math. Lifted verbatim from the
`measure_link_cost` script (NEU-297) so the measurement harness and production telemetry
(NEU-375) price identical dollars from the same constants. Adding a new model means adding
its `Rates` to `_RATES` — `rates_for` raises `KeyError` on an unknown model rather than
silently mispricing it."""

from dataclasses import dataclass

from upmovies.llm.client import Usage

# Cache pricing multipliers (relative to base input): 5-min ephemeral write = 1.25x,
# read = 0.10x. Batch path applies a flat 50% discount on the whole total.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10
_BATCH_DISCOUNT = 0.50


@dataclass(frozen=True)
class Rates:
    """Per-million-token USD rates. VERIFY against https://www.anthropic.com/pricing before
    trusting the $ figures — raw token counts are the recorded source of truth."""

    input_per_mtok: float
    output_per_mtok: float


# VERIFY current rates before trusting $ output.
HAIKU_4_5 = Rates(input_per_mtok=1.00, output_per_mtok=5.00)
SONNET_4_6 = Rates(input_per_mtok=3.00, output_per_mtok=15.00)

# model id -> rates. Add a model here when a stage starts using it (else rates_for raises).
_RATES: dict[str, Rates] = {
    "claude-haiku-4-5": HAIKU_4_5,
    "claude-sonnet-4-6": SONNET_4_6,
}


def rates_for(model: str) -> Rates:
    """Look up the per-mtok rates for a model id. Raises KeyError on an unknown model —
    adding a model to the pipeline means adding its Rates to `_RATES`."""
    return _RATES[model]


def price(usage: Usage, rates: Rates, *, batch: bool) -> float:
    """Dollar cost of `usage` at `rates`. Cache writes cost 1.25x base input, cache reads
    0.10x; the batch path applies a flat 50% discount on the whole total."""
    base_in = rates.input_per_mtok / 1_000_000
    out = rates.output_per_mtok / 1_000_000
    cost = (
        usage.input_tokens * base_in
        + usage.cache_creation_input_tokens * base_in * _CACHE_WRITE_MULT
        + usage.cache_read_input_tokens * base_in * _CACHE_READ_MULT
        + usage.output_tokens * out
    )
    return cost * (_BATCH_DISCOUNT if batch else 1.0)
