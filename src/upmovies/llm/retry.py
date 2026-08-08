"""One retry policy, used by every adapter.

Shared rather than merely matched (spec §9). Two adapters configured to similar-looking
numbers still retry differently — different status sets, different backoff curves, different
readings of `Retry-After` — and once they do, every latency and coverage comparison between
two providers is partly measuring the retry loops and reporting it as provider quality.

It matters more here than the usual "retries are nice to have", because under-retrying does
not surface as an error. `_link_stage_sequential` catches per chunk, records `failed_delta`
and continues (`link/pipeline.py:71-93`), so a provider that gives up one attempt early loses
a silent chunk of 15 stories — which an eval run reads as "this provider links fewer stories".

Note that `httpx.AsyncHTTPTransport(retries=N)` does **not** do this job: it retries
connection *establishment* only, and 429/5xx is most of what needs retrying.

Deliberately free of DB, vendor SDK and HTTP-client imports — the two adapters differ in how
they get a status code and headers off a failure, and that difference belongs in each of them
rather than here."""

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

# Transient by nature: the same request stands a real chance of succeeding shortly. Anything
# else — a malformed request, a bad credential, a missing model — fails identically four times
# and is better raised at once.
_RETRYABLE_STATUS = frozenset({408, 409, 429})


@dataclass(frozen=True)
class Retry:
    """A classifier's verdict that a failure is worth another attempt.

    A verdict object rather than a bare `bool` so it can carry the provider's own `Retry-After`
    hint; `None` from a classifier means "do not retry"."""

    retry_after: float | None = None


Classifier = Callable[[BaseException], Retry | None]


@dataclass(frozen=True)
class RetryPolicy:
    """How many attempts, how long each may take, and how long to wait between them.

    The defaults reproduce what the Anthropic SDK was already configured with
    (`max_retries=3, timeout=60.0`), so adopting the shared loop changes which code runs the
    retries without changing how the Anthropic path behaves."""

    max_retries: int = 3
    timeout: float = 60.0
    initial_backoff: float = 0.5
    max_backoff: float = 8.0
    jitter: float = 0.25
    max_retry_after: float = 60.0

    def delay_for(
        self,
        attempt: int,
        retry_after: float | None = None,
        *,
        rand: Callable[[], float] = random.random,
    ) -> float:
        """Seconds to wait after `attempt` (1-based) before trying again.

        A `Retry-After` wins over the curve — the provider knows its own rate-limit window
        better than a guess does — but is **clamped** to `max_retry_after` rather than
        discarded when it exceeds it. Discarding it would drop the wait back to the curve's
        first step, so a provider asking for two minutes would be retried within half a second
        and 429'd every time, spending the whole retry budget inside a window it had already
        said was closed. That is the silently-dropped chunk of 15 stories in spec §9, arrived
        at by trying *harder*. Waiting less than asked is the one option guaranteed to fail;
        capping is at worst a wasted attempt. A nonsensical value (zero or negative) carries no
        information and falls back to the curve.

        Otherwise: exponential, capped at `max_backoff`, then shortened by up to `jitter` of
        itself so that a chunked stage whose calls failed together does not retry in lockstep."""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.max_retry_after)
        backoff = min(self.max_backoff, self.initial_backoff * 2 ** (attempt - 1))
        return backoff * (1.0 - self.jitter * rand())


DEFAULT_RETRY_POLICY = RetryPolicy()


class Attempts:
    """How many HTTP attempts one logical call has made so far.

    Mutable and passed in, rather than returned, because the count is needed most on the path
    where nothing is returned: an exhausted call re-raises, and the adapter still has to record
    `attempts` on the telemetry row it writes before letting the exception through. One
    instance per logical call — sharing one across concurrent calls would interleave counts."""

    __slots__ = ("made",)

    def __init__(self) -> None:
        self.made = 0


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """The `Retry-After` header in seconds, or None when absent or not expressed that way.

    The HTTP-date form is deliberately not parsed: it needs a clock and a timezone to mean
    anything, both providers in scope send integer seconds, and falling back to the backoff
    curve is the safe failure — a slightly wrong wait, never a wrong number of attempts."""
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def retry_for_status(status: int, headers: Mapping[str, str] | None) -> Retry | None:
    """The shared verdict on an HTTP status. Both adapters route their status-bearing failures
    through this, which is what makes them retry the same things rather than similar things."""
    if status in _RETRYABLE_STATUS or status >= 500:
        return Retry(retry_after=retry_after_seconds(headers))
    return None


async def call_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    classify: Classifier,
    attempts: Attempts,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Run `operation` until it succeeds, a failure is judged not worth retrying, or the
    retries run out — recording every attempt on `attempts` as it goes.

    The original exception propagates untouched. Four call sites handle provider failure
    differently today and all of them dispatch on the exception (spec §12), so wrapping it in
    a retry-flavoured error here would quietly change behaviour at every one of them."""
    while True:
        attempts.made += 1
        try:
            return await operation()
        except Exception as exc:
            verdict = classify(exc)
            if verdict is None or attempts.made > policy.max_retries:
                raise
            await sleep(policy.delay_for(attempts.made, verdict.retry_after, rand=rand))
