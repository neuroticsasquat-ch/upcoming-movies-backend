"""Thin async wrapper over the Anthropic Messages API with prompt caching. Self-contained
(no DB); callers pass system content blocks + messages and get the response text back.
Mirrors the TMDB client's shape: an async context manager that returns plain data."""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic


def cached_system_block(text: str) -> dict[str, Any]:
    """A system content block marked for ephemeral prompt caching. Put the stable prefix
    (e.g. the film roster) in one of these so repeated calls reuse the cached tokens."""
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def _concat_text(blocks: list[Any]) -> str:
    """Concatenate the text of an Anthropic response's content blocks."""
    return "".join(block.text for block in blocks if block.type == "text")


@dataclass(frozen=True)
class Usage:
    """Token counts for one Messages/Batch call. Cache fields are 0 when no caching
    occurred. `__add__` lets callers `sum(usages, Usage())` across many calls."""

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


@dataclass(frozen=True)
class BatchRequest:
    """One request in a Message Batch. Mirrors `complete()`'s args plus a `custom_id`.
    `system` carries the `cached_system_block(...)` prefix so the roster stays cacheable."""

    custom_id: str
    model: str
    system: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    max_tokens: int = 4096


@dataclass(frozen=True)
class BatchResult:
    """One request's outcome. `text` is set when `ok`; otherwise `error_type` /
    `error_message` describe the failure (errored / expired / canceled / missing).
    `usage` carries the call's token counts when `ok`, else `None`."""

    custom_id: str
    ok: bool
    text: str = ""
    error_type: str | None = None
    error_message: str | None = None
    usage: Usage | None = None


def _to_result(entry: Any) -> BatchResult:
    """Map one batch results-stream entry to a `BatchResult`."""
    result = entry.result
    if result.type == "succeeded":
        return BatchResult(
            custom_id=entry.custom_id,
            ok=True,
            text=_concat_text(result.message.content),
            usage=Usage.from_sdk(result.message.usage),
        )
    if result.type == "errored":
        err = result.error.error
        return BatchResult(
            custom_id=entry.custom_id,
            ok=False,
            error_type=err.type,
            error_message=err.message,
        )
    # "canceled" | "expired" — fall-through for any other terminal type the API may add
    return BatchResult(custom_id=entry.custom_id, ok=False, error_type=result.type)


class AnthropicClient:
    """Async context manager over `AsyncAnthropic`. Call surfaces: `complete` and
    `complete_batch`."""

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

    async def complete_batch(
        self,
        requests: list[BatchRequest],
        *,
        poll_interval: float = 15.0,
        timeout: float = 3600.0,
    ) -> dict[str, BatchResult]:
        """Submit one Message Batch, poll to terminal, return per-request results keyed by
        `custom_id`. A failed request surfaces as `BatchResult(ok=False, ...)` and never
        loses the other requests' results. Returns `{}` immediately for an empty input.

        Note: `timeout=0` raises `TimeoutError` immediately — it is not a "poll once"
        pattern. Use the default (3600 s) or a positive value for production callers."""
        if not requests:
            return {}

        batch = await self._client.messages.batches.create(
            requests=[  # type: ignore[arg-type]
                {
                    "custom_id": r.custom_id,
                    "params": {
                        "model": r.model,
                        "max_tokens": r.max_tokens,
                        "system": r.system,
                        "messages": r.messages,
                    },
                }
                for r in requests
            ]
        )

        deadline = time.monotonic() + timeout
        while batch.processing_status != "ended":
            if time.monotonic() >= deadline:
                raise TimeoutError(f"batch {batch.id} did not reach 'ended' within {timeout}s")
            await asyncio.sleep(poll_interval)
            batch = await self._client.messages.batches.retrieve(batch.id)

        # Pre-seed every custom_id so an omitted result surfaces instead of silently dropping.
        results: dict[str, BatchResult] = {
            r.custom_id: BatchResult(
                custom_id=r.custom_id,
                ok=False,
                error_type="missing",
                error_message="no result returned for request",
            )
            for r in requests
        }

        results_stream = await self._client.messages.batches.results(batch.id)
        async for entry in results_stream:
            results[entry.custom_id] = _to_result(entry)
        return results
