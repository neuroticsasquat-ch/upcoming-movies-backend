"""The retry policy is *shared* by both adapters, not configured to similar values in each
(spec §9). These tests pin the shared pieces — which statuses are worth another attempt, how
long to wait, and how the attempt count survives an exception — so that a latency or coverage
comparison between two providers is measuring the providers rather than two retry loops that
happen to look alike.
"""

import httpx
import pytest

from upmovies.llm.retry import (
    DEFAULT_RETRY_POLICY,
    Attempts,
    Retry,
    RetryPolicy,
    call_with_retry,
    retry_after_seconds,
    retry_for_status,
)


class _RecordingSleep:
    """Stands in for `asyncio.sleep`: records what the loop would have waited, waits nothing."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class _Boom(Exception):
    pass


def _always_retry(_exc: BaseException) -> Retry | None:
    return Retry()


def _never_retry(_exc: BaseException) -> Retry | None:
    return None


# --- the policy itself --------------------------------------------------------------------


def test_the_default_policy_keeps_todays_anthropic_numbers():
    """3 retries / 60s was the SDK configuration the Anthropic path already ran under
    (`client.py:113-114`); the shared policy adopts it rather than inventing new numbers, so
    nothing about the Anthropic path's behaviour changes when it stops using the SDK's loop."""
    assert DEFAULT_RETRY_POLICY.max_retries == 3
    assert DEFAULT_RETRY_POLICY.timeout == 60.0


def test_backoff_is_exponential_and_bounded():
    policy = RetryPolicy(initial_backoff=0.5, max_backoff=2.0, jitter=0.0)
    delays = [policy.delay_for(attempt) for attempt in (1, 2, 3, 4, 5)]
    assert delays == [0.5, 1.0, 2.0, 2.0, 2.0]


def test_jitter_shortens_the_wait_by_at_most_its_ratio():
    policy = RetryPolicy(initial_backoff=1.0, jitter=0.25)
    assert policy.delay_for(1, rand=lambda: 1.0) == pytest.approx(0.75)
    assert policy.delay_for(1, rand=lambda: 0.0) == pytest.approx(1.0)


def test_retry_after_is_honoured_over_the_backoff_curve():
    policy = RetryPolicy(initial_backoff=0.5, jitter=0.0)
    assert policy.delay_for(1, retry_after=7.0) == 7.0


def test_an_unaffordable_retry_after_is_clamped_rather_than_discarded():
    """A provider asking us to wait an hour would hang a nightly publish, so the wait is
    capped — but capped, not thrown away. Dropping back to the curve would retry within half a
    second a provider that just said "not for two minutes", burn all four attempts inside the
    window it had already closed, and lose the chunk (spec §9). Waiting less than asked is the
    one option guaranteed to fail."""
    policy = RetryPolicy(initial_backoff=0.5, jitter=0.0, max_retry_after=60.0)
    assert policy.delay_for(1, retry_after=3600.0) == 60.0
    assert policy.delay_for(1, retry_after=120.0) == 60.0


def test_a_nonsensical_retry_after_falls_back_to_the_backoff_curve():
    """Zero or negative carries no information about when to come back, so the curve decides."""
    policy = RetryPolicy(initial_backoff=0.5, jitter=0.0)
    assert policy.delay_for(1, retry_after=-1.0) == 0.5
    assert policy.delay_for(1, retry_after=0.0) == 0.5


# --- header parsing -----------------------------------------------------------------------


def test_retry_after_seconds_reads_an_integer_header():
    assert retry_after_seconds({"retry-after": "12"}) == 12.0


def test_retry_after_seconds_is_case_insensitive_over_httpx_headers():
    assert retry_after_seconds(httpx.Headers({"Retry-After": "3"})) == 3.0


def test_retry_after_seconds_declines_the_http_date_form():
    """The HTTP-date form needs a clock and a timezone to mean anything; the providers in scope
    send integer seconds, and falling back to exponential backoff is the safe failure."""
    assert retry_after_seconds({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None


def test_retry_after_seconds_of_a_missing_header_is_none():
    assert retry_after_seconds({}) is None
    assert retry_after_seconds(None) is None


# --- which statuses are worth another attempt ---------------------------------------------


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 529])
def test_transient_statuses_are_retryable(status: int):
    assert retry_for_status(status, {}) is not None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retryable(status: int):
    """Retrying a malformed request or a bad credential just spends the same failure four
    times. Both adapters draw the line in the same place because they draw it here."""
    assert retry_for_status(status, {}) is None


def test_a_retryable_status_carries_the_providers_retry_after():
    verdict = retry_for_status(429, {"retry-after": "4"})
    assert verdict == Retry(retry_after=4.0)


# --- the loop -----------------------------------------------------------------------------


async def test_a_call_that_succeeds_first_time_is_one_attempt_and_no_waiting():
    sleep = _RecordingSleep()
    attempts = Attempts()

    async def op() -> str:
        return "ok"

    out = await call_with_retry(
        op,
        policy=DEFAULT_RETRY_POLICY,
        classify=_always_retry,
        attempts=attempts,
        sleep=sleep,
    )

    assert out == "ok"
    assert attempts.made == 1
    assert sleep.delays == []


async def test_a_retryable_failure_is_retried_then_succeeds():
    sleep = _RecordingSleep()
    attempts = Attempts()
    outcomes = [_Boom(), "ok"]

    async def op() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    out = await call_with_retry(
        op,
        policy=RetryPolicy(initial_backoff=0.5, jitter=0.0),
        classify=_always_retry,
        attempts=attempts,
        sleep=sleep,
    )

    assert out == "ok"
    assert attempts.made == 2
    assert sleep.delays == [0.5]


async def test_exhaustion_re_raises_the_original_exception_after_max_retries():
    """The exception type must survive the loop untouched: four call sites handle provider
    failure differently today (spec §12) and all of them dispatch on it."""
    sleep = _RecordingSleep()
    attempts = Attempts()

    async def op() -> str:
        raise _Boom("still broken")

    with pytest.raises(_Boom):
        await call_with_retry(
            op,
            policy=RetryPolicy(max_retries=3, initial_backoff=0.0),
            classify=_always_retry,
            attempts=attempts,
            sleep=sleep,
        )

    # 1 initial attempt + 3 retries, and a wait before each retry but not after the last.
    assert attempts.made == 4
    assert len(sleep.delays) == 3


async def test_a_non_retryable_failure_is_raised_on_the_first_attempt():
    sleep = _RecordingSleep()
    attempts = Attempts()

    async def op() -> str:
        raise _Boom("nope")

    with pytest.raises(_Boom):
        await call_with_retry(
            op,
            policy=DEFAULT_RETRY_POLICY,
            classify=_never_retry,
            attempts=attempts,
            sleep=sleep,
        )

    assert attempts.made == 1
    assert sleep.delays == []


async def test_the_classifiers_retry_after_drives_the_wait():
    sleep = _RecordingSleep()
    attempts = Attempts()
    calls = {"n": 0}

    async def op() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Boom("slow down")
        return "ok"

    await call_with_retry(
        op,
        policy=RetryPolicy(initial_backoff=0.5, jitter=0.0),
        classify=lambda _exc: Retry(retry_after=9.0),
        attempts=attempts,
        sleep=sleep,
    )

    assert sleep.delays == [9.0]


async def test_max_retries_zero_makes_a_single_attempt():
    attempts = Attempts()

    async def op() -> str:
        raise _Boom("once")

    with pytest.raises(_Boom):
        await call_with_retry(
            op,
            policy=RetryPolicy(max_retries=0),
            classify=_always_retry,
            attempts=attempts,
            sleep=_RecordingSleep(),
        )

    assert attempts.made == 1
