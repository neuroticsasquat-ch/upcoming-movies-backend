"""Thin async wrapper over the Anthropic Messages API with prompt caching. Self-contained
(no DB); callers pass system content blocks + messages and get the response text back.
Mirrors the TMDB client's shape: an async context manager that returns plain data."""

import time
from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class CallResult:
    """Everything one *logical* API call is worth recording — retries folded in, not split out.

    `latency_ms` is total wall-clock for the logical call, retries included: that is the number
    the pipeline actually experiences, and the one that decides whether a provider is viable for
    a daily publish. `attempts` is kept separate so retry behaviour stays visible rather than
    hidden inside the latency figure.

    `parse_ok` is not the adapter's to fill in — the adapter has no idea whether the caller will
    parse the text, let alone how. The call site stamps it via `CallLog.set_parse_ok` after its
    own parse, and it stays None where no parse happens."""

    text: str = ""
    usage: Usage = Usage()
    latency_ms: int = 0
    attempts: int = 1
    ok: bool = True
    error_type: str | None = None
    parse_ok: bool | None = None


class CallLog:
    """The per-unit-of-work accumulator a *stage* owns, passes down to the service function, and
    persists afterwards.

    Why a log rather than just returning `CallResult` upward: a call that fails must still be
    recorded, and today's failure isolation depends on the exception continuing to propagate
    from four call sites that each handle it differently. A returned value cannot do both. The
    stage keeps the log outside its `try`, so whatever the call recorded survives the exception
    and still becomes a row.

    Deliberately DB-free (spec §4): it holds plain dataclasses, and `ingest.runs` is what turns
    them into `ingest.llm_call` rows."""

    def __init__(self) -> None:
        self._results: list[CallResult] = []

    def record(self, result: CallResult) -> CallResult:
        """Append a result and hand it back, so an adapter can `return calls.record(...)`."""
        self._results.append(result)
        return result

    def set_parse_ok(self, ok: bool) -> None:
        """Stamp `parse_ok` on the most recently recorded call. Raises when nothing has been
        recorded — parsing output that was never fetched is a programming error, not a NULL."""
        if not self._results:
            raise ValueError("set_parse_ok called before any call was recorded")
        self._results[-1] = replace(self._results[-1], parse_ok=ok)

    @property
    def results(self) -> tuple[CallResult, ...]:
        return tuple(self._results)

    @property
    def usage(self) -> Usage:
        """Summed token usage across every recorded call — the single source the per-stage
        `run_llm_usage` aggregate is built from, so it reconciles against the per-call rows by
        construction rather than by coincidence."""
        return sum((r.usage for r in self._results), Usage())


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


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

    async def complete_call(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
        calls: CallLog,
    ) -> CallResult:
        """One logical Messages call, recorded into `calls` and returned.

        A provider failure is recorded with `ok=False` and an `error_type` *before* the
        exception is re-raised, so the caller's failure handling is unchanged and the call
        still leaves a telemetry row. Timing spans the whole logical call, SDK retries
        included; `with_raw_response` is what makes the retry count observable."""
        started = time.perf_counter()
        try:
            raw = await self._client.messages.with_raw_response.create(
                model=model,
                system=system,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
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

    async def complete_with_usage(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> tuple[str, Usage]:
        """Like `complete` but also returns the call's token `Usage` (incl. cache reads/
        writes). The measurement harness uses this; pipeline stages use `complete_call`, which
        additionally records what `ingest.llm_call` needs."""
        result = await self.complete_call(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            calls=CallLog(),
        )
        return result.text, result.usage

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
