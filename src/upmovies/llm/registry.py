"""Which providers exist and where the OpenAI-compatible ones live.

Base URLs are constants here rather than settings (spec §8): they are a property of the
provider, not of a deployment, and tests reach the adapters by mocking the transport rather
than by pointing them at a different host. Making them configurable would add an env var whose
only correct value is the one written below, and a second one to get wrong.

The provider strings are load-bearing beyond this module — `pricing.rates_for(provider, model)`
is keyed on them — so they are named constants and not string literals scattered per call site.
"""

ANTHROPIC = "anthropic"
DEEPINFRA = "deepinfra"
DEEPSEEK = "deepseek"

PROVIDERS: tuple[str, ...] = (ANTHROPIC, DEEPINFRA, DEEPSEEK)

# Anthropic is deliberately absent: it is reached through its own SDK, which owns its endpoint.
# Listing it would imply `OpenAICompatClient` could serve it, which it cannot.
_OPENAI_COMPAT_BASE_URLS: dict[str, str] = {
    DEEPINFRA: "https://api.deepinfra.com/v1/openai",
    DEEPSEEK: "https://api.deepseek.com/v1",
}


def base_url_for(provider: str) -> str:
    """Base URL of an OpenAI-compatible provider. Raises `KeyError` for anything else — same
    discipline as `rates_for`: a provider nobody has written an entry for is a mistake worth
    crashing on, not one worth guessing an endpoint for."""
    return _OPENAI_COMPAT_BASE_URLS[provider]
