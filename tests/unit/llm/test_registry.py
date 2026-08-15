"""Base URLs are constants, not configuration (spec §8): they are not deployment-varying, and
tests mock at the transport layer rather than by pointing a client somewhere else."""

import pytest

from upmovies.llm import registry


def test_the_openai_compatible_providers_have_base_urls():
    assert registry.base_url_for(registry.DEEPSEEK).startswith("https://")
    assert registry.base_url_for(registry.DEEPINFRA).startswith("https://")


def test_anthropic_has_no_base_url_here():
    """Anthropic is reached through its SDK, which owns its own endpoint. Listing it here would
    imply the OpenAI-compatible adapter could serve it, which it cannot."""
    with pytest.raises(KeyError):
        registry.base_url_for(registry.ANTHROPIC)


def test_an_unknown_provider_raises():
    with pytest.raises(KeyError):
        registry.base_url_for("openrouter")


def test_every_named_provider_is_in_providers():
    assert set(registry.PROVIDERS) == {
        registry.ANTHROPIC,
        registry.DEEPINFRA,
        registry.DEEPSEEK,
    }


def test_provider_names_match_the_pricing_table_keys():
    """`rates_for(provider, model)` is keyed on these exact strings, so a typo in either place
    is a `KeyError` at the worst possible moment — during an eval run, after the tokens are
    spent. Pinning them together is cheaper than discovering it there."""
    from upmovies.llm.pricing import _RATES

    assert {provider for provider, _model in _RATES} <= set(registry.PROVIDERS)
