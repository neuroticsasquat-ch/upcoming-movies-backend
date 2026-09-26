"""Unit tests for the per-IP token bucket and the pieces around it (NEU-1344).

The clock is injected everywhere rather than slept on: a bucket that refills at 0.083 tokens
a minute takes twelve minutes to hand back one token, and no test can wait for that.
"""

import logging

import pytest
from fastapi import Request

from upmovies.app.rate_limit import (
    ANON_KEY,
    BUCKET_SETTINGS,
    CLIENT_IP_HEADER,
    MAX_TRACKED_KEYS,
    ORIGIN_HEADER,
    BucketLimit,
    MemoryStore,
    RateLimitConfigurationError,
    client_ip,
    limit_for,
    parse_bucket_limit,
    reset_warnings,
    validate_rate_limit_configuration,
)
from upmovies.config import Settings


@pytest.fixture(autouse=True)
def _forget_warnings():
    """`_warned` is process-wide by design — a deployment fault is said once, not once a
    request — so each test that asserts on one starts from silence."""
    reset_warnings()
    yield
    reset_warnings()


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
        "ADMIN_TOKEN": "t",
        "TMDB_API_KEY": "k",
        "ANTHROPIC_API_KEY": "k",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _request(
    *,
    client: tuple[str, int] | None = ("10.0.0.1", 5000),
    headers: dict[str, str] | None = None,
) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/feed",
        "headers": raw,
        "client": client,
    }
    return Request(scope)  # type: ignore[arg-type]


# --- the bucket itself ---------------------------------------------------------------


def test_a_burst_within_capacity_is_allowed():
    store = MemoryStore(clock=FakeClock())
    limit = BucketLimit(capacity=5, refill_per_minute=1)
    assert all(store.take("signup:1.2.3.4", limit).allowed for _ in range(5))


def test_the_request_past_capacity_is_refused_with_a_retry_after():
    clock = FakeClock()
    store = MemoryStore(clock=clock)
    # One token a minute back: the sixth request waits a full minute for the next one.
    limit = BucketLimit(capacity=5, refill_per_minute=1)
    for _ in range(5):
        store.take("signup:1.2.3.4", limit)
    decision = store.take("signup:1.2.3.4", limit)
    assert not decision.allowed
    assert decision.retry_after == 60


def test_retry_after_counts_down_as_the_bucket_refills():
    clock = FakeClock()
    store = MemoryStore(clock=clock)
    limit = BucketLimit(capacity=1, refill_per_minute=1)
    assert store.take("k", limit).allowed
    assert store.take("k", limit).retry_after == 60
    clock.advance(45)
    assert store.take("k", limit).retry_after == 15


def test_retry_after_is_never_zero_for_a_refusal():
    """A `Retry-After: 0` invites an immediate retry that would also be refused."""
    clock = FakeClock()
    store = MemoryStore(clock=clock)
    limit = BucketLimit(capacity=1, refill_per_minute=120)
    assert store.take("k", limit).allowed
    clock.advance(0.49)  # 0.98 tokens: still short, but under a second's worth
    decision = store.take("k", limit)
    assert not decision.allowed
    assert decision.retry_after == 1


def test_tokens_refill_at_the_configured_rate():
    clock = FakeClock()
    store = MemoryStore(clock=clock)
    limit = BucketLimit(capacity=5, refill_per_minute=0.083)
    for _ in range(5):
        assert store.take("k", limit).allowed
    assert not store.take("k", limit).allowed
    clock.advance(12 * 60)  # 12 minutes at 0.083/min is just under one token
    assert not store.take("k", limit).allowed
    clock.advance(60)
    assert store.take("k", limit).allowed


def test_the_bucket_never_refills_past_capacity():
    clock = FakeClock()
    store = MemoryStore(clock=clock)
    limit = BucketLimit(capacity=3, refill_per_minute=60)
    assert store.take("k", limit).allowed
    clock.advance(3600)
    assert sum(store.take("k", limit).allowed for _ in range(10)) == 3


def test_keys_are_independent():
    store = MemoryStore(clock=FakeClock())
    limit = BucketLimit(capacity=1, refill_per_minute=1)
    assert store.take("signup:1.1.1.1", limit).allowed
    assert not store.take("signup:1.1.1.1", limit).allowed
    assert store.take("signup:2.2.2.2", limit).allowed


def test_the_store_evicts_the_least_recently_used_key_when_full():
    store = MemoryStore(clock=FakeClock(), max_keys=2)
    limit = BucketLimit(capacity=1, refill_per_minute=1)
    store.take("a", limit)  # a spends its token
    store.take("b", limit)
    store.take("a", limit)  # touches `a`, so `b` is now the least recently used
    store.take("c", limit)  # evicts `b`
    assert len(store) == 2
    # `a` is still exhausted; `b`, having been dropped, comes back as a fresh bucket. Read
    # `a` first: taking from `b` re-inserts it and evicts whatever is oldest by then.
    assert not store.take("a", limit).allowed
    assert store.take("b", limit).allowed


def test_the_default_key_bound_is_fifty_thousand():
    assert MAX_TRACKED_KEYS == 50_000


# --- bucket configuration ------------------------------------------------------------


def test_parse_reads_capacity_and_refill():
    assert parse_bucket_limit("5/0.083", bucket="signup") == BucketLimit(
        capacity=5.0, refill_per_minute=0.083
    )


@pytest.mark.parametrize(
    "raw",
    ["", "5", "5/", "/1", "5/1/1", "five/1", "5/one", "0/1", "-1/1", "5/0", "5/-1"],
)
def test_a_malformed_bucket_string_is_refused(raw):
    with pytest.raises(RateLimitConfigurationError):
        parse_bucket_limit(raw, bucket="signup")


def test_every_bucket_has_a_settings_field_and_a_usable_default():
    settings = _settings()
    for bucket in BUCKET_SETTINGS:
        limit = limit_for(settings, bucket)
        assert limit.capacity >= 1
        assert limit.refill_per_minute > 0


def test_the_documented_buckets_are_the_registered_ones():
    """Spec §2. A route asking for a bucket that is not here fails at import."""
    assert set(BUCKET_SETTINGS) == {
        "signup",
        "login",
        "auth_request",
        "import",
        "public",
        "ics",
        "digest_unsubscribe",
    }


def test_validation_names_every_bad_bucket_at_once():
    settings = _settings(RATE_LIMIT_SIGNUP="nope", RATE_LIMIT_LOGIN="also-nope")
    with pytest.raises(RateLimitConfigurationError) as err:
        validate_rate_limit_configuration(settings)
    assert "RATE_LIMIT_SIGNUP" in str(err.value)
    assert "RATE_LIMIT_LOGIN" in str(err.value)


def test_validation_passes_on_the_defaults():
    validate_rate_limit_configuration(_settings())


# --- resolving the client IP ---------------------------------------------------------


def test_client_ip_is_the_socket_address_by_default():
    assert client_ip(_request(), _settings()) == "10.0.0.1"


def test_a_clientless_scope_falls_back_to_the_anon_key(caplog):
    with caplog.at_level(logging.WARNING):
        assert client_ip(_request(client=None), _settings()) == ANON_KEY
        assert client_ip(_request(client=None), _settings()) == ANON_KEY
    # Said once per process, not once per request: the fault is the deployment's, and at one
    # line a request a misconfigured proxy would be the whole log.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert ANON_KEY in warnings[0].getMessage()


def test_a_signed_request_keys_on_the_forwarded_visitor_ip():
    settings = _settings(SSR_ORIGIN_SECRET="s3cret")
    request = _request(headers={ORIGIN_HEADER: "s3cret", CLIENT_IP_HEADER: "203.0.113.9"})
    assert client_ip(request, settings) == "203.0.113.9"


def test_an_ipv6_visitor_ip_is_accepted():
    settings = _settings(SSR_ORIGIN_SECRET="s3cret")
    request = _request(headers={ORIGIN_HEADER: "s3cret", CLIENT_IP_HEADER: "2001:db8::1"})
    assert client_ip(request, settings) == "2001:db8::1"


def test_a_wrong_secret_is_ignored_rather_than_rejected():
    settings = _settings(SSR_ORIGIN_SECRET="s3cret")
    request = _request(headers={ORIGIN_HEADER: "guess", CLIENT_IP_HEADER: "203.0.113.9"})
    assert client_ip(request, settings) == "10.0.0.1"


def test_the_forwarded_header_is_ignored_when_no_secret_is_configured():
    request = _request(headers={ORIGIN_HEADER: "", CLIENT_IP_HEADER: "203.0.113.9"})
    assert client_ip(request, _settings()) == "10.0.0.1"


@pytest.mark.parametrize("value", ["", "not-an-ip", "203.0.113.9, 70.0.0.1"])
def test_a_signed_request_with_a_malformed_visitor_ip_falls_back(value, caplog):
    settings = _settings(SSR_ORIGIN_SECRET="s3cret")
    request = _request(headers={ORIGIN_HEADER: "s3cret", CLIENT_IP_HEADER: value})
    with caplog.at_level(logging.WARNING):
        assert client_ip(request, settings) == "10.0.0.1"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert CLIENT_IP_HEADER in warnings[0].getMessage()


def test_a_non_ascii_signature_is_ignored_rather_than_raising():
    """`hmac.compare_digest` refuses `str` outside ASCII, and Starlette decodes headers as
    latin-1 — so before this was compared as bytes, one high byte in a forged header was a 500
    on every limited route rather than the documented "ignored, never rejected"."""
    settings = _settings(SSR_ORIGIN_SECRET="s3cret")
    request = _request(headers={ORIGIN_HEADER: "s3crét", CLIENT_IP_HEADER: "203.0.113.9"})
    assert client_ip(request, settings) == "10.0.0.1"


def test_a_secret_matching_byte_for_byte_is_honoured_whatever_it_spells():
    """The other half of the above: a non-ASCII secret still *works*, so the fix is a change
    of comparison, not a silent narrowing of what may be configured."""
    settings = _settings(SSR_ORIGIN_SECRET="s3crét")
    request = _request(headers={ORIGIN_HEADER: "s3crét", CLIENT_IP_HEADER: "203.0.113.9"})
    assert client_ip(request, settings) == "203.0.113.9"


def test_a_signed_request_with_no_visitor_ip_header_falls_back():
    settings = _settings(SSR_ORIGIN_SECRET="s3cret")
    request = _request(headers={ORIGIN_HEADER: "s3cret"})
    assert client_ip(request, settings) == "10.0.0.1"
