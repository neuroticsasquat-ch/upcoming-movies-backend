"""OpenAI-compatible adapter: one `Completer` covering DeepInfra and DeepSeek.

A thin first-party client over the `httpx` already in the dependency list and already OTel-
instrumented, rather than a routing library — the reasoning is ADR-0007's to record (NEU-982,
not yet written). The endpoint surface actually used is one POST with four fields, so what a
library would add here is mostly its own pricing table shadowing `pricing.py`'s deliberate
`KeyError` on an unknown model.

Providers differ in more than their base URL, and the differences that matter are in `usage`:
see `_usage_from`."""

import time
from collections.abc import Mapping
from typing import Any

import httpx

from upmovies.llm.registry import base_url_for
from upmovies.llm.retry import (
    DEFAULT_RETRY_POLICY,
    Attempts,
    Retry,
    RetryPolicy,
    call_with_retry,
    retry_for_status,
)
from upmovies.llm.types import CallLog, CallResult, Prompt, UnsupportedPromptError, Usage


def _to_wire(model: str, prompt: Prompt) -> dict[str, Any]:
    """Realize the neutral contract in the OpenAI chat shape.

    There is no `cache_control` to place: these providers cache automatically, on the longest
    byte-identical request prefix. The adapter's whole caching obligation is therefore
    *position* — the stable prefix leads as the system message, unmodified, and the varying
    payload follows. Nothing here decides whether caching engages, and nothing can: that
    depends on each provider's minimum cacheable prefix (spec §5.1)."""
    if prompt.prefill is not None:
        raise UnsupportedPromptError(
            "the OpenAI chat completions API has no assistant-prefill equivalent; "
            "this prompt cannot be served by an OpenAI-compatible provider"
        )
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.stable_prefix},
            {"role": "user", "content": prompt.user},
        ],
        "max_tokens": prompt.max_tokens,
    }
    if prompt.json_object:
        body["response_format"] = {"type": "json_object"}
    return body


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _text_from(payload: Mapping[str, Any]) -> str:
    """The assistant's message content, or "" when the response carries none.

    Empty rather than an exception on purpose: the four stages already disagree about what a
    useless response means — `link` raises, `cluster` catches, `summarize` recovers,
    `source_judge` warns — and unifying that is an explicit non-goal (spec §12). Handing each
    one an empty string lets its own parser reach its own verdict."""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


def _usage_from(usage: Mapping[str, Any]) -> Usage:
    """Map an OpenAI-compatible `usage` block onto `Usage`, whichever way the provider reports
    cached tokens — the one place the two providers genuinely diverge.

    Two shapes are in circulation. DeepSeek splits the prompt explicitly into
    `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`; everyone following OpenAI reports
    `prompt_tokens_details.cached_tokens` alongside a `prompt_tokens` total that *includes*
    them. `Usage`'s four counts must be disjoint or `price` charges the cached tokens twice —
    once at base and once at the cache-read rate — so the inclusive total has the cached count
    subtracted, and the explicit miss count is preferred where a provider offers it.

    `cache_creation_input_tokens` stays 0 by construction: automatic prefix caching has no
    explicit write, so there is nothing to charge a write premium for. That is exactly why
    `cache_write_mult` stopped being an Anthropic constant (spec §7). DeepInfra does report a
    `prompt_tokens_details.cache_write_tokens`, observed only as `null` — it stays unmapped,
    because those tokens are already inside `prompt_tokens` and moving them across without also
    subtracting them would break the disjointness `price` depends on. At `cache_write_mult=1.0`
    it prices identically either way, so mapping it would buy risk and no accuracy.

    A provider reporting no cached counts at all fails the project's hard selection criterion
    (spec §10) — but that is a finding for the evaluation to report, not a reason to refuse the
    response here, so it maps to "everything was uncached". Both providers in scope pass it;
    `scripts/verify_provider_capabilities.py` is what establishes that, against live endpoints."""
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    # Absent and zero are different answers, so this resolves on `is not None` rather than on
    # truthiness: a genuine cache miss reports `prompt_cache_hit_tokens: 0`, and reading that
    # as "field not present" would fall through to whatever the other shape happened to say.
    hit = usage.get("prompt_cache_hit_tokens")
    cache_read = int(hit if hit is not None else details.get("cached_tokens") or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    uncached = int(miss) if miss is not None else max(prompt_tokens - cache_read, 0)
    return Usage(
        input_tokens=uncached,
        output_tokens=int(usage.get("completion_tokens") or 0),
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=0,
    )


def _classify(exc: BaseException) -> Retry | None:
    """Which httpx failures are worth another attempt. The status verdict is deferred to
    `retry_for_status` so it is literally the same judgement the Anthropic adapter makes."""
    if isinstance(exc, httpx.HTTPStatusError):
        return retry_for_status(exc.response.status_code, exc.response.headers)
    if isinstance(exc, httpx.TransportError):
        # Connect errors, read errors and timeouts — the request never got an answer, so
        # nothing is known about whether it would fail again.
        return Retry()
    # Anything else reached us from a response that did arrive (a body that would not decode,
    # most likely). Asking again buys the same broken body.
    return None


class OpenAICompatClient:
    """Async context manager over one provider's chat completions endpoint.

    Call surfaces mirror `AnthropicClient` exactly — `complete`, `complete_with_usage` and the
    telemetry-bearing `complete_call` — because the point of the seam is that a stage cannot
    tell which one it is holding."""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ):
        self._policy = policy
        self._client = httpx.AsyncClient(
            # Raises KeyError for a provider with no entry, at construction rather than at the
            # first call — a misconfigured stage should fail before it starts spending tokens.
            base_url=base_url_for(provider),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=policy.timeout,
        )

    async def __aenter__(self) -> "OpenAICompatClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the underlying connection pool. Separate from `__aexit__` because `Gateway`
        builds its clients lazily and so cannot enter them as context managers — it closes
        them by name instead (`llm/gateway.py`)."""
        await self._client.aclose()

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/chat/completions", json=body)
        response.raise_for_status()
        return response.json()

    async def complete_call(self, *, model: str, prompt: Prompt, calls: CallLog) -> CallResult:
        """One logical chat completion, recorded into `calls` and returned.

        A provider failure is recorded with `ok=False` and an `error_type` *before* the
        exception is re-raised, so the caller's failure handling is unchanged and the call
        still leaves a telemetry row. Timing spans the whole logical call, retries and their
        backoff included: that is the latency the pipeline actually experiences, and the number
        that decides whether a provider is viable for a daily publish.

        A prompt this provider cannot serve is rejected before any of that — it never reached
        the provider, so it is a programming error rather than a call worth recording."""
        body = _to_wire(model, prompt)
        attempts = Attempts()
        started = time.perf_counter()
        try:
            payload = await call_with_retry(
                lambda: self._post(body),
                policy=self._policy,
                classify=_classify,
                attempts=attempts,
            )
            # Reading the body is inside the try on purpose: a 200 that doesn't decode is still
            # a call that was made, took time and cost tokens.
            result = CallResult(
                text=_text_from(payload),
                usage=_usage_from(payload.get("usage") or {}),
                latency_ms=_elapsed_ms(started),
                attempts=attempts.made,
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
        """Like `complete` but also returns the call's token `Usage`. Measurement harnesses use
        this; pipeline stages use `complete_call`, which additionally records the telemetry."""
        result = await self.complete_call(model=model, prompt=prompt, calls=CallLog())
        return result.text, result.usage

    async def complete(self, *, model: str, prompt: Prompt) -> str:
        """One chat completion. Returns the assistant message's text."""
        text, _ = await self.complete_with_usage(model=model, prompt=prompt)
        return text
