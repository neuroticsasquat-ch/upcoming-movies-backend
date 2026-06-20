"""Thin async wrapper over the Anthropic Messages API with prompt caching. Self-contained
(no DB); callers pass system content blocks + messages and get the response text back.
Mirrors the TMDB client's shape: an async context manager that returns plain data."""

from typing import Any

from anthropic import AsyncAnthropic


def cached_system_block(text: str) -> dict[str, Any]:
    """A system content block marked for ephemeral prompt caching. Put the stable prefix
    (e.g. the film roster) in one of these so repeated calls reuse the cached tokens."""
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


class AnthropicClient:
    """Async context manager over `AsyncAnthropic`. One call surface: `complete`."""

    def __init__(self, api_key: str, *, max_retries: int = 3, timeout: float = 60.0):
        self._client = AsyncAnthropic(api_key=api_key, max_retries=max_retries, timeout=timeout)

    async def __aenter__(self) -> "AnthropicClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.close()

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
        resp = await self._client.messages.create(
            model=model,
            system=system,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
        )
        return "".join(block.text for block in resp.content if block.type == "text")
