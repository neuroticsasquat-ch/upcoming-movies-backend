from dataclasses import FrozenInstanceError

import pytest

from upmovies.llm.client import Usage
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
    # cache write = 1.25x input, cache read = 0.10x input → 1.25 + 0.10 = $1.35
    u = Usage(cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert price(u, HAIKU_4_5, batch=False) == 1.35


def test_price_batch_halves_total():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price(u, HAIKU_4_5, batch=True) == 3.0


def test_rates_for_known_models():
    assert rates_for("claude-haiku-4-5") is HAIKU_4_5
    assert rates_for("claude-sonnet-4-6") is SONNET_4_6


def test_rates_for_unknown_model_raises_keyerror():
    with pytest.raises(KeyError):
        rates_for("claude-opus-9-9")


def test_sonnet_is_three_times_haiku_input():
    assert SONNET_4_6.input_per_mtok == 3.0
    assert SONNET_4_6.output_per_mtok == 15.0


def test_rates_is_frozen():
    r = Rates(input_per_mtok=1.0, output_per_mtok=5.0)
    with pytest.raises(FrozenInstanceError):
        r.input_per_mtok = 2.0  # type: ignore[misc]
