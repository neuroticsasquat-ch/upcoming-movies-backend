from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from upmovies.llm import Usage
from upmovies.llm.pricing import (
    HAIKU_4_5,
    SONNET_4_6,
    Rates,
    price,
    rates_for,
)


def test_price_full_rates_sequential():
    # 1,000,000 uncached input @ $1 + 1,000,000 output @ $5 = $6.00
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price(u, HAIKU_4_5, batch=False) == 6.0


def test_price_cache_multipliers():
    # Anthropic: cache write = 1.25x input, cache read = 0.10x input → 1.25 + 0.10 = $1.35
    u = Usage(cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert price(u, HAIKU_4_5, batch=False) == 1.35


def test_price_uses_the_entrys_own_cache_multipliers():
    """`price` reads the multipliers off `Rates`, so a provider with different cache economics
    prices the same token counts differently — the whole point of moving them per-entry."""
    no_write_premium = Rates(
        input_per_mtok=1.00,
        output_per_mtok=5.00,
        cache_write_mult=1.00,
        cache_read_mult=0.02,
    )
    u = Usage(cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert price(u, no_write_premium, batch=False) == 1.02


def test_price_batch_halves_total():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price(u, HAIKU_4_5, batch=True) == 3.0


def test_rates_for_known_pairs():
    assert rates_for("anthropic", "claude-haiku-4-5") is HAIKU_4_5
    assert rates_for("anthropic", "claude-sonnet-4-6") is SONNET_4_6


def test_rates_for_unknown_pair_raises_keyerror():
    with pytest.raises(KeyError):
        rates_for("anthropic", "claude-opus-9-9")


def test_rates_for_known_model_under_wrong_provider_raises_keyerror():
    """The model id alone is not a key. A provider that does not serve this model must raise
    rather than fall back to whoever else happens to serve it."""
    with pytest.raises(KeyError):
        rates_for("deepinfra", "claude-haiku-4-5")


def test_same_weights_on_two_hosts_are_separate_entries():
    """The collision the rekey exists to prevent: DeepSeek's own API and DeepInfra both serve
    DeepSeek V4 Pro, at different prices and under different cache economics."""
    first_party = rates_for("deepseek", "deepseek-v4-pro")
    reseller = rates_for("deepinfra", "deepseek-ai/DeepSeek-V4-Pro")
    assert first_party != reseller
    assert first_party.input_per_mtok != reseller.input_per_mtok


@pytest.mark.parametrize(
    ("provider", "model", "cache_write_mult", "cache_read_mult"),
    [
        # Automatic caching: the provider matches the longest byte-identical prefix, so there
        # is no explicit write to charge for. Read multipliers are the published cached-input
        # price over the published base input price, both verified 2026-08-08.
        ("deepseek", "deepseek-v4-flash", 1.0, 0.0028 / 0.14),
        ("deepseek", "deepseek-v4-pro", 1.0, 0.003625 / 0.435),
        ("deepinfra", "deepseek-ai/DeepSeek-V4-Flash", 1.0, 0.018 / 0.09),
        ("deepinfra", "deepseek-ai/DeepSeek-V4-Pro", 1.0, 0.10 / 1.30),
    ],
)
def test_automatic_caching_entries_state_their_own_cache_economics(
    provider: str, model: str, cache_write_mult: float, cache_read_mult: float
):
    """Pinned per entry rather than derived from the provider name. A future provider that
    *does* charge for cache writes states its own economics in its `Rates` — the required
    field is what stops it inheriting Anthropic's, so it must not have to fail a rule here."""
    rates = rates_for(provider, model)
    assert rates.cache_write_mult == cache_write_mult
    assert rates.cache_read_mult == pytest.approx(cache_read_mult)


def test_sonnet_is_three_times_haiku_input():
    assert SONNET_4_6.input_per_mtok == 3.0
    assert SONNET_4_6.output_per_mtok == 15.0


def test_rates_is_frozen():
    r = Rates(
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        cache_write_mult=1.25,
        cache_read_mult=0.10,
    )
    with pytest.raises(FrozenInstanceError):
        r.input_per_mtok = 2.0  # type: ignore[misc]


def test_rates_requires_all_four_fields():
    """No defaults, deliberately: a new provider must state its own cache economics rather
    than silently inheriting Anthropic's."""
    with pytest.raises(TypeError):
        Rates(input_per_mtok=1.0, output_per_mtok=5.0)  # type: ignore[call-arg]


# Real `ingest.run_llm_usage` rows read from prod on 2026-08-08, before this rekey — the four
# token types, both Anthropic models, and both `batched` modes. `cost_usd` is `numeric(12,6)`,
# so agreeing within half a microdollar is an exact match at the precision the row was stored
# with. Any drift in the Anthropic rates or the cache multipliers moves these by whole cents.
_HISTORICAL_ROWS = [
    ("claude-haiku-4-5", True, 2_465_699, 486_556, 1_872_816, 16_035_987, "12.565372"),
    ("claude-haiku-4-5", True, 1_897_946, 369_127, 472_368, 12_208_896, "9.525969"),
    ("claude-haiku-4-5", False, 50_015, 28_327, 1_162_824, 44_724, "0.363837"),
    ("claude-haiku-4-5", False, 34_761, 19_054, 1_007_182, 59_246, "0.304807"),
    ("claude-sonnet-4-6", True, 483_177, 10_573, 0, 0, "0.804063"),
    ("claude-sonnet-4-6", False, 24_542, 977, 0, 0, "0.088281"),
]


@pytest.mark.parametrize(
    ("model", "batched", "input_tokens", "output_tokens", "cache_read", "cache_write", "cost_usd"),
    _HISTORICAL_ROWS,
)
def test_historical_rows_reprice_to_their_recorded_cost(
    model: str,
    batched: bool,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    cost_usd: str,
):
    usage = Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )
    recomputed = price(usage, rates_for("anthropic", model), batch=batched)
    assert recomputed == pytest.approx(float(Decimal(cost_usd)), abs=5e-7)
