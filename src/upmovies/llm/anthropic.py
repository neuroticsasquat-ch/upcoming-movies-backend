"""Anthropic adapter: turns a neutral `Prompt` into a Messages API call and returns plain
data. Self-contained (no DB); mirrors the TMDB client's shape as an async context manager.

This module is the *only* place Anthropic's wire format appears. Callers hand it a `Prompt`
and never name `system` blocks, `cache_control` or assistant turns — see `types.Prompt` for
the contract that replaced them.

(The module-level `from anthropic import ...` below resolves to the SDK, not to this file:
Python 3 has no implicit relative imports, so `upmovies.llm.anthropic` does not shadow it.)"""

import time
from typing import Any

from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic

from upmovies.llm.retry import (
    DEFAULT_RETRY_POLICY,
    Attempts,
    Retry,
    RetryPolicy,
    call_with_retry,
    retry_for_status,
)
from upmovies.llm.types import CallLog, CallResult, Prompt, Usage


def _to_wire(prompt: Prompt) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Realize the neutral contract in Anthropic's shape: the stable prefix becomes the one
    leading system block, marked for ephemeral caching, and the varying payload the user turn.

    The `cache_control` marker is unconditional on purpose. Anthropic caches only above a
    per-model prefix floor (Haiku 4.5 = 4096 tokens, Sonnet 4.6 = 2048) and silently no-ops
    below it — so deciding whether to mark would mean estimating token counts here, or worse,
    asking each builder to estimate its own, which is exactly the vendor knowledge the `Prompt`
    DTO exists to delete from them. Marking always is free when it no-ops and correct when a
    prefix grows past the floor.

    `prompt.json_object` has no counterpart here — Anthropic has no `response_format` — so it is
    ignored rather than approximated. The call sites' hand-rolled JSON extractors are what has
    always got them JSON out of this provider, and they stay."""
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


def _truncated_from(resp: Any) -> bool | None:
    """Whether the reply stopped because it hit `max_tokens`, or None when the message does not
    say. Anthropic's `stop_reason: "max_tokens"` is the same predicate the OpenAI-compatible
    providers spell `finish_reason: "length"` — see `CallResult.truncated` for why the predicate
    is what gets stored rather than either spelling (NEU-1014)."""
    reason = getattr(resp, "stop_reason", None)
    if reason is None:
        return None
    return reason == "max_tokens"


def _classify(exc: BaseException) -> Retry | None:
    """Which SDK failures are worth another attempt. The status verdict is deferred to
    `retry_for_status` so it is literally the same judgement the OpenAI-compatible adapter
    makes — see `retry.py` for why sharing it, rather than matching it, is the point."""
    if isinstance(exc, APIStatusError):
        return retry_for_status(exc.status_code, exc.response.headers)
    if isinstance(exc, APIConnectionError):
        # Timeouts arrive as `APITimeoutError`, a subclass: the request never got an answer, so
        # nothing is known about whether it would fail again.
        return Retry()
    return None


class AnthropicClient:
    """Async context manager over `AsyncAnthropic`. Call surfaces: `complete`,
    `complete_with_usage`, and `complete_call` (the telemetry-bearing one)."""

    def __init__(self, api_key: str, *, policy: RetryPolicy = DEFAULT_RETRY_POLICY):
        self._policy = policy
        # `max_retries=0` disables the SDK's own retry loop deliberately. It is a perfectly
        # good loop — it was this path's retry policy until now — but leaving it in place would
        # mean the two adapters retried under separately-configured policies that merely looked
        # alike, and a provider comparison would then be partly measuring the loops (spec §9).
        self._client = AsyncAnthropic(api_key=api_key, max_retries=0, timeout=policy.timeout)

    async def __aenter__(self) -> "AnthropicClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying connection pool. Separate from `__aexit__` because `Gateway`
        builds its clients lazily and so cannot enter them as context managers — it closes
        them by name instead (`llm/gateway.py`)."""
        await self._client.close()

    async def complete_call(self, *, model: str, prompt: Prompt, calls: CallLog) -> CallResult:
        """One logical Messages call, recorded into `calls` and returned.

        A provider failure is recorded with `ok=False` and an `error_type` *before* the
        exception is re-raised, so the caller's failure handling is unchanged and the call
        still leaves a telemetry row. Timing spans the whole logical call, retries and their
        backoff included."""
        system, messages = _to_wire(prompt)
        attempts = Attempts()
        started = time.perf_counter()
        try:
            resp = await call_with_retry(
                # Deserialization happens inside the SDK call, and therefore inside the try, on
                # purpose: a 200 whose body doesn't validate is still a call that was made,
                # took time and cost tokens.
                lambda: self._client.messages.create(
                    model=model,
                    system=system,  # type: ignore[arg-type]
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=prompt.max_tokens,
                ),
                policy=self._policy,
                classify=_classify,
                attempts=attempts,
            )
            result = CallResult(
                text=_concat_text(resp.content),
                usage=Usage.from_sdk(resp.usage),
                latency_ms=_elapsed_ms(started),
                attempts=attempts.made,
                truncated=_truncated_from(resp),
            )
        except Exception as exc:
            calls.record(
                CallResult(
                    latency_ms=_elapsed_ms(started),
                    attempts=max(attempts.made, 1),
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
