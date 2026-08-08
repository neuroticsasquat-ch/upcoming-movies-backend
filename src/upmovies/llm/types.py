"""Provider-neutral LLM types: the request DTO, what a call is worth recording, and the
surface a stage needs from whatever is answering it.

Deliberately free of DB, SQLAlchemy and vendor SDK imports (spec §4) — these are the types
both adapters and all four stages share, so anything vendor-shaped belongs one layer down."""

from dataclasses import dataclass, replace
from typing import Any, Protocol


@dataclass(frozen=True)
class Prompt:
    """One request, described by what it *requires* rather than by how a vendor provides it.

    The requirement is **stable content first, byte-identical across calls**. That single
    invariant covers two mechanisms that look incompatible: Anthropic caches explicitly, up
    to a placed `cache_control` breakpoint, while DeepInfra and DeepSeek cache automatically
    on the longest byte-identical request prefix. Naming the requirement rather than either
    mechanism is what keeps the vendor out of the four builders (spec §5.1).

    Every adapter is obligated to serialize `stable_prefix` first and unmodified. Content put
    there is a promise by the builder that it does not vary call to call; content that varies
    belongs in `user`, or the promise is broken and no provider's cache will match.

    Whether caching actually engages is not the builder's problem and not knowable from here:
    each provider has a minimum cacheable prefix (Haiku 4.5's is 4096 tokens). Below it the
    contract is inert — which is the anticipated outcome for all four stages today and is a
    success, not a failure (spec §3). It costs nothing and engages by itself if a prefix grows.

    `prefill` seeds the assistant turn to force a continuation rather than a preamble;
    `summarize` relies on it to get a JSON envelope back, and `parse_summary` is written to
    read that continuation. An adapter whose provider cannot prefill must therefore **raise**
    rather than drop it: honouring the rest of the request and silently discarding this part
    would hand the parser a shape it does not expect, at the one stage that recovers from bad
    JSON rather than failing loudly.
    """

    stable_prefix: str
    user: str
    prefill: str | None = None
    max_tokens: int = 4096


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


class Completer(Protocol):
    """The LLM surface a stage needs: one logical call, recorded into the caller's `CallLog`.

    Token usage rides along inside the recorded `CallResult` rather than being returned
    separately, so the per-call telemetry rows and the per-stage `run_llm_usage` aggregate are
    built from the same numbers (NEU-975).

    It lives here rather than beside a stage because it is the *provider* side of the seam, and
    a copy per stage was already how `link` and `synthesize` drifted into declaring the same
    Protocol twice."""

    async def complete_call(self, *, model: str, prompt: Prompt, calls: CallLog) -> CallResult: ...
