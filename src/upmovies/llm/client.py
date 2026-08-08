"""Anthropic adapter: turns a neutral `Prompt` into a Messages API call and returns plain
data. Self-contained (no DB); mirrors the TMDB client's shape as an async context manager.

This module is the *only* place Anthropic's wire format appears. Callers hand it a `Prompt`
and never name `system` blocks, `cache_control` or assistant turns — see `types.Prompt` for
the contract that replaced them."""

import time
from typing import Any

from anthropic import AsyncAnthropic

from upmovies.llm.types import CallLog, CallResult, Prompt, Usage


def _to_wire(prompt: Prompt) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Realize the neutral contract in Anthropic's shape: the stable prefix becomes the one
    leading system block, marked for ephemeral caching, and the varying payload the user turn.

    The `cache_control` marker is unconditional on purpose. Anthropic caches only above a
    per-model prefix floor (Haiku 4.5 = 4096 tokens, Sonnet 4.6 = 2048) and silently no-ops
    below it — so deciding whether to mark would mean estimating token counts here, or worse,
    asking each builder to estimate its own, which is exactly the vendor knowledge the `Prompt`
    DTO exists to delete from them. Marking always is free when it no-ops and correct when a
    prefix grows past the floor."""
    system = [
        {"type": "text", "text": prompt.stable_prefix, "cache_control": {"type": "ephemeral"}}
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt.user}]
    if prompt.prefill is not None:
        messages.append({"role": "assistant", "content": prompt.prefill})
    return system, messages


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _concat_text(blocks: list[Any]) -> str:
    """Concatenate the text of an Anthropic response's content blocks."""
    return "".join(block.text for block in blocks if block.type == "text")


def _attempts_from_error(exc: BaseException) -> int:
    """How many HTTP attempts a failed call made. The SDK owns the retry loop until the shared
    policy lands (spec §9) and stamps every attempt's request with `x-stainless-retry-count`;
    reading it back off the failed request is the only place that count survives an exception.
    Falls back to 1 when no request is attached."""
    request = getattr(exc, "request", None)
    header = getattr(request, "headers", {}).get("x-stainless-retry-count")
    if not isinstance(header, str):
        return 1
    try:
        return int(header) + 1
    except ValueError:
        return 1


class AnthropicClient:
    """Async context manager over `AsyncAnthropic`. Call surfaces: `complete`,
    `complete_with_usage`, and `complete_call` (the telemetry-bearing one)."""

    def __init__(self, api_key: str, *, max_retries: int = 3, timeout: float = 60.0):
        self._client = AsyncAnthropic(api_key=api_key, max_retries=max_retries, timeout=timeout)

    async def __aenter__(self) -> "AnthropicClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.close()

    async def complete_call(self, *, model: str, prompt: Prompt, calls: CallLog) -> CallResult:
        """One logical Messages call, recorded into `calls` and returned.

        A provider failure is recorded with `ok=False` and an `error_type` *before* the
        exception is re-raised, so the caller's failure handling is unchanged and the call
        still leaves a telemetry row. Timing spans the whole logical call, SDK retries
        included; `with_raw_response` is what makes the retry count observable."""
        system, messages = _to_wire(prompt)
        started = time.perf_counter()
        try:
            raw = await self._client.messages.with_raw_response.create(
                model=model,
                system=system,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                max_tokens=prompt.max_tokens,
            )
            # Deserialization is inside the try on purpose: a 200 whose body doesn't validate
            # is still a call that was made, took time and cost tokens.
            resp = raw.parse()
            result = CallResult(
                text=_concat_text(resp.content),
                usage=Usage.from_sdk(resp.usage),
                latency_ms=_elapsed_ms(started),
                attempts=raw.retries_taken + 1,
            )
        except Exception as exc:
            calls.record(
                CallResult(
                    latency_ms=_elapsed_ms(started),
                    attempts=_attempts_from_error(exc),
                    ok=False,
                    error_type=type(exc).__name__,
                )
            )
            raise
        return calls.record(result)

    async def complete_with_usage(self, *, model: str, prompt: Prompt) -> tuple[str, Usage]:
        """Like `complete` but also returns the call's token `Usage` (incl. cache reads/
        writes). The measurement harness uses this; pipeline stages use `complete_call`, which
        additionally records what `ingest.llm_call` needs."""
        result = await self.complete_call(model=model, prompt=prompt, calls=CallLog())
        return result.text, result.usage

    async def complete(self, *, model: str, prompt: Prompt) -> str:
        """One Messages call. Returns the concatenated text of the response content blocks."""
        text, _ = await self.complete_with_usage(model=model, prompt=prompt)
        return text
