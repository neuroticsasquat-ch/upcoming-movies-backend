"""Thin async wrapper over the Anthropic Messages API with prompt caching. Self-contained
(no DB); callers pass system content blocks + messages and get the response text back.
Mirrors the TMDB client's shape: an async context manager that returns plain data."""

from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic


def cached_system_block(text: str) -> dict[str, Any]:
    """A system content block marked for ephemeral prompt caching. Put the stable prefix
    (e.g. the film roster) in one of these so repeated calls reuse the cached tokens.

    Caching only engages above a per-model prefix floor: Haiku 4.5 = 4096 tokens,
    Sonnet 4.6 = 2048 tokens. Below the floor `cache_control` silently no-ops
    (cache_creation_input_tokens == 0). Per stage (NEU-377): LINK uses this and caches
    iff `instructions + roster` exceeds 4096 tok; CLUSTER does NOT (per-film payload,
    instructions under 2048 — uses a plain block); SUMMARIZE does NOT (instructions
    under 4096 — uses a plain block)."""
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def _concat_text(blocks: list[Any]) -> str:
    """Concatenate the text of an Anthropic response's content blocks."""
    return "".join(block.text for block in blocks if block.type == "text")


@dataclass(frozen=True)
class Usage:
    """Token counts for one Messages call. Cache fields are 0 when no caching occurred.
    `__add__` lets callers `sum(usages, Usage())` across many calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
        )

    @classmethod
    def from_sdk(cls, usage: Any) -> "Usage":
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )


class AnthropicClient:
    """Async context manager over `AsyncAnthropic`. Call surfaces: `complete` and
    `complete_with_usage`."""

    def __init__(self, api_key: str, *, max_retries: int = 3, timeout: float = 60.0):
        self._client = AsyncAnthropic(api_key=api_key, max_retries=max_retries, timeout=timeout)

    async def __aenter__(self) -> "AnthropicClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.close()

    async def complete_with_usage(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> tuple[str, Usage]:
        """Like `complete` but also returns the call's token `Usage` (incl. cache reads/
        writes). The measurement harness uses this; production callers use `complete`."""
        resp = await self._client.messages.create(
            model=model,
            system=system,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
        )
        return _concat_text(resp.content), Usage.from_sdk(resp.usage)

    async def complete(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> str:
        """One Messages call. `system` is a list of content blocks — use
        `cached_system_block` for the cacheable prefix. Returns the concatenated text of
        the response content blocks."""
        text, _ = await self.complete_with_usage(
            model=model, system=system, messages=messages, max_tokens=max_tokens
        )
        return text
