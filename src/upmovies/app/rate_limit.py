"""Per-IP token buckets for the public surface: one dependency, seven named buckets (D-19).

Open signup (NEU-1343) removed the invite gate, which was the only thing metering account
creation. Nothing else on the anonymous surface was metered at all — the per-email login
lockout (`app.LoginAttempt`) counts failures for *one address*, not requests from one host,
so it says nothing about a script working through a list. This module is the meter, and it is
per IP because that is the only identity an anonymous caller has.

**The store is in-process.** The API runs a single uvicorn process (`Dockerfile`, no
`--workers`), so a dict is not an approximation of the count — it *is* the count. The
`RateLimitStore` protocol is here so a Postgres-backed store can be added the day the API runs
replicas, and no such store is written now: one would cost a write on every public read to
solve a problem this deployment does not have.

**Two things make the key trustworthy**, and both are deployment shape rather than code:

1. The prod and dev CMDs pass `--proxy-headers --forwarded-allow-ips='*'`, so
   `request.client.host` is the address Traefik saw rather than Traefik itself. Trusting every
   hop is safe because the container port is reachable only over the Docker network. (This
   also fixes `login_attempt.ip`, which has been recording the proxy.)
2. The site is server-rendered on a Cloudflare Worker, whose loaders fetch this API from a
   handful of shared egress IPs. Keying those on the socket address would throttle every
   anonymous visitor through one bucket, so a request carrying the shared `SSR_ORIGIN_SECRET`
   in `X-Backlotter-Origin` may name its visitor in `X-Backlotter-Client-IP` instead. The
   visitor IP travels in a dedicated header rather than `X-Forwarded-For` because Traefik
   strips forwarded headers from senders outside its trusted set, and the Worker is outside it.

An invalid signature is **ignored, never rejected**: the request is keyed on its socket address
and served. A forged header buys the sender a bucket of their own, which is what they already
had; refusing would turn a wrong secret — the state every deploy is in before the Worker
sibling ships (NEU-1389) — into an outage.
"""

import ipaddress
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from hmac import compare_digest
from math import ceil
from typing import Protocol

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from upmovies.config import Settings, get_settings

log = logging.getLogger(__name__)

# The Worker's two headers. `X-Forwarded-For` is deliberately not one of them — see the module
# docstring — and nothing outside this module reads either.
ORIGIN_HEADER = "X-Backlotter-Origin"
CLIENT_IP_HEADER = "X-Backlotter-Client-IP"

# The key for a request whose address cannot be resolved at all. Behind Traefik this should
# never happen; it is reachable in-process (a scope with no `client`), and one shared bucket is
# a better answer than no limit.
ANON_KEY = "anon"

# What `MemoryStore` will hold before it starts evicting. At ~100 bytes a bucket this is a few
# megabytes, and the eviction it bounds is safe: a dropped key is a full bucket, so the worst
# an attacker buys by cycling 50k addresses is the capacity they would have had anyway.
MAX_TRACKED_KEYS = 50_000

# Each bucket and the `Settings` field holding its `"<capacity>/<refill_per_minute>"` string.
# The mapping is what makes `rate_limit("signup")` fail at import rather than at request time,
# and what `validate_rate_limit_configuration` walks at boot. Buckets with no route yet
# (`import` is M3, `ics` is M7) are registered here now so that the deployment's env is
# complete before the routes land — on Coolify a variable absent at first deploy is one
# somebody has to add by hand later (`AGENTS.md`).
BUCKET_SETTINGS: dict[str, str] = {
    "signup": "rate_limit_signup",
    "login": "rate_limit_login",
    "auth_request": "rate_limit_auth_request",
    "import": "rate_limit_import",
    "public": "rate_limit_public",
    "ics": "rate_limit_ics",
    "digest_unsubscribe": "rate_limit_digest_unsubscribe",
}

# The buckets `RATE_LIMIT_PUBLIC_ENABLED` gates (spec §5). Only `public` is here: the flag
# exists for the ordering problem — the backend deploys before the Worker signs its requests,
# and until it does every anonymous visitor shares the Worker's egress IPs — and nothing
# reaches `/calendar/{token}.ics` through the Worker.
PUBLIC_GATED_BUCKETS = frozenset({"public"})


class RateLimitConfigurationError(ValueError):
    """A bucket string that cannot be read as `"<capacity>/<refill_per_minute>"`.

    Raised at boot from the lifespan, in the manner of `MailConfigurationError`: a limiter
    configured with nonsense is either no limit or a locked door, and both are worse
    discovered by the container than by the first caller."""


@dataclass(frozen=True, slots=True)
class BucketLimit:
    """A bucket's size and how fast it fills: burst capacity, plus a sustained rate."""

    capacity: float
    refill_per_minute: float


@dataclass(frozen=True, slots=True)
class Decision:
    """`retry_after` is meaningful only when `allowed` is False, and is always >= 1 there."""

    allowed: bool
    retry_after: int


class RateLimitStore(Protocol):
    """Where the buckets live.

    `take` is one atomic "refill, then spend a token if there is one". The limit is passed in
    rather than held by the store because the store is bucket-agnostic — the bucket name is
    part of the key, and the numbers come from settings, which a Postgres implementation would
    want passed the same way."""

    def take(self, key: str, limit: BucketLimit) -> Decision: ...


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class MemoryStore:
    """The one implementation: a bounded LRU of buckets against a monotonic clock.

    Synchronous and lock-free on purpose. It runs inside an asyncio event loop with no `await`
    between reading a bucket and writing it back, so the whole of `take` is already atomic with
    respect to other requests; a lock would add contention to protect against interleaving that
    cannot occur.

    `time.monotonic` rather than wall time so an NTP step cannot hand out free tokens (or
    freeze a bucket for an hour). The clock is injected because the slowest bucket refills at
    0.083 tokens a minute, which no test can wait for."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    def take(self, key: str, limit: BucketLimit) -> Decision:
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=limit.capacity, updated_at=now)
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(
                limit.capacity, bucket.tokens + elapsed * limit.refill_per_minute / 60
            )
            bucket.updated_at = now
        self._buckets[key] = bucket
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return Decision(allowed=True, retry_after=0)
        # Seconds until the bucket holds one whole token, rounded up and floored at 1: a
        # `Retry-After: 0` invites a retry that would be refused again.
        shortfall = 1 - bucket.tokens
        return Decision(
            allowed=False,
            retry_after=max(1, ceil(shortfall * 60 / limit.refill_per_minute)),
        )


_default_store = MemoryStore()


def get_rate_limit_store() -> RateLimitStore:
    """The process-wide store, as a dependency so a test can override it.

    A module global rather than `app.state` (where the mailer lives), because the limiter has
    to work on a route reached without the lifespan — which is how most of the suite, and every
    `ASGITransport` client in it, reaches one."""
    return _default_store


def parse_bucket_limit(raw: str, *, bucket: str) -> BucketLimit:
    """Read `"<capacity>/<refill_per_minute>"`, e.g. `"5/0.083"` — five now, five an hour."""
    capacity_raw, sep, refill_raw = raw.partition("/")
    if not sep:
        raise RateLimitConfigurationError(
            f"RATE_LIMIT_{bucket.upper()} is {raw!r}: expected '<capacity>/<refill_per_minute>'"
        )
    try:
        capacity = float(capacity_raw)
        refill = float(refill_raw)
    except ValueError as err:
        raise RateLimitConfigurationError(
            f"RATE_LIMIT_{bucket.upper()} is {raw!r}: both halves must be numbers"
        ) from err
    if capacity < 1:
        raise RateLimitConfigurationError(
            f"RATE_LIMIT_{bucket.upper()} is {raw!r}: capacity must be at least 1, or the "
            f"bucket refuses every request"
        )
    if refill <= 0:
        # Zero would also be a division by zero in `Retry-After`, but the reason to refuse it
        # is that it means "never refills": the first burst closes the route until restart.
        raise RateLimitConfigurationError(
            f"RATE_LIMIT_{bucket.upper()} is {raw!r}: refill_per_minute must be above 0"
        )
    return BucketLimit(capacity=capacity, refill_per_minute=refill)


def limit_for(settings: Settings, bucket: str) -> BucketLimit:
    """The configured `(capacity, refill_per_minute)` for a registered bucket.

    Parsed per call rather than cached: it is a `partition` and two `float`s against a route
    that is about to touch Postgres, and caching it would mean a `Settings` overridden in a
    test silently answering with the previous one's numbers."""
    return parse_bucket_limit(getattr(settings, BUCKET_SETTINGS[bucket]), bucket=bucket)


def validate_rate_limit_configuration(settings: Settings) -> None:
    """Assert every bucket string parses. Called from the lifespan, beside the LLM and mail
    checks, so a typo'd limit kills the container at boot rather than the route at its first
    caller.

    Collects every fault before raising, for the reason `validate_mail_configuration` does: a
    deploy that mis-set two of them should learn about both from one failed boot."""
    problems: list[str] = []
    for bucket in BUCKET_SETTINGS:
        try:
            limit_for(settings, bucket)
        except RateLimitConfigurationError as err:
            problems.append(str(err))
    if problems:
        raise RateLimitConfigurationError(
            "rate limit configuration is unusable:\n  " + "\n  ".join(problems)
        )


# Faults that are a property of the *deployment* rather than of the request: they would repeat
# on every call, so they are said once per process and then kept quiet.
_warned: set[str] = set()


def reset_warnings() -> None:
    """Forget what has already been warned about. For tests only — `_warned` is process-wide,
    so without this the second test to exercise a one-time warning asserts on silence."""
    _warned.clear()


def _warn_once(key: str, message: str, *args: object) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message, *args)


def _forwarded_visitor_ip(request: Request) -> str | None:
    """The visitor IP a signed request names, or None if it names nothing usable.

    Parsed with `ipaddress` rather than taken as text because the value becomes a bucket key:
    an unvalidated header is an unbounded key space, and a comma-joined `X-Forwarded-For`-style
    list pasted in here would key each hop-combination separately."""
    raw = request.headers.get(CLIENT_IP_HEADER)
    if raw is None:
        # Not a fault on its own — the Worker sibling ships separately (NEU-1389), and until it
        # does a signed request is simply one nobody sends.
        return None
    candidate = raw.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        _warn_once(
            "malformed-client-ip",
            "%s on a signed request is %r, which is not an IP address: keying on the socket "
            "address instead",
            CLIENT_IP_HEADER,
            raw,
        )
        return None
    return candidate


def client_ip(request: Request, settings: Settings) -> str:
    """The address to meter this request against.

    The socket address — which is the real caller, given `--proxy-headers` — unless the request
    carries the SSR secret, in which case it is the visitor the Worker named. Every limiter key
    comes through here, and nothing else in the codebase reads either header.

    Takes `settings` explicitly where the spec wrote `client_ip(request)`: the secret has to
    come from somewhere, and reaching for `get_settings()` inside would make the one function
    a test most wants to drive with a handful of different secrets the one function it cannot,
    since the route's own `Settings` arrives through a dependency override."""
    socket_ip = request.client.host if request.client else None
    secret = settings.ssr_origin_secret
    if secret:
        presented = request.headers.get(ORIGIN_HEADER)
        # Constant-time, because the comparison is against a shared secret and the header is
        # attacker-controlled — and over *bytes*, because `compare_digest` raises `TypeError`
        # on a `str` holding anything outside ASCII. Starlette decodes headers as latin-1, so
        # one non-ASCII byte in a forged header would otherwise be a 500 on every limited
        # route the moment `SSR_ORIGIN_SECRET` is set. A wrong secret is ignored, never
        # rejected, and that has to include a wrong secret spelled in high bytes.
        #
        # `latin-1` back out, not `utf-8`: it is the exact inverse of the decode Starlette
        # just did, so what is compared is the bytes that came down the wire against the
        # bytes the configured secret is written in.
        if presented is not None and compare_digest(
            presented.encode("latin-1", "replace"), secret.encode("utf-8")
        ):
            forwarded = _forwarded_visitor_ip(request)
            if forwarded is not None:
                return forwarded
    if socket_ip is None:
        _warn_once(
            "no-client-address",
            "request has no client address; rate limiting it under the shared %r key",
            ANON_KEY,
        )
        return ANON_KEY
    return socket_ip


class RateLimited(Exception):
    """Raised by the dependency, rendered by `rate_limited_handler`.

    Not an `HTTPException`: FastAPI's handler renders that as `{"detail": ...}`, and the body
    this answers with carries the bucket and the wait alongside it, so a client can tell which
    limit it hit and how long it is out for without parsing prose."""

    def __init__(self, *, bucket: str, retry_after: int) -> None:
        super().__init__(f"rate limited on {bucket} for {retry_after}s")
        self.bucket = bucket
        self.retry_after = retry_after


async def rate_limited_handler(request: Request, exc: Exception) -> JSONResponse:
    """The 429. Registered on the app in `create_app`.

    Typed against bare `Exception` because that is the signature Starlette's handler registry
    expects; the narrowing assert is the price of it."""
    assert isinstance(exc, RateLimited)
    return JSONResponse(
        status_code=429,
        content={
            "detail": "rate_limited",
            "bucket": exc.bucket,
            "retry_after": exc.retry_after,
        },
        headers={"Retry-After": str(exc.retry_after)},
    )


def rate_limit(bucket: str) -> Callable[..., None]:
    """The dependency for one bucket: `Depends(rate_limit("signup"))`.

    ```python
    @router.post("/signup", dependencies=[Depends(rate_limit("signup"))])
    ```

    An unregistered name raises here, at import, rather than on the first request — a route
    wired to a bucket that does not exist would otherwise be a route with no limit at all,
    which is exactly the failure this module exists to prevent."""
    if bucket not in BUCKET_SETTINGS:
        raise KeyError(
            f"unknown rate limit bucket {bucket!r}: expected one of "
            f"{', '.join(sorted(BUCKET_SETTINGS))}"
        )

    def dependency(
        request: Request,
        settings: Settings = Depends(get_settings),
        store: RateLimitStore = Depends(get_rate_limit_store),
    ) -> None:
        if not settings.rate_limit_enabled:
            return
        if bucket in PUBLIC_GATED_BUCKETS and not settings.rate_limit_public_enabled:
            return
        key = client_ip(request, settings)
        decision = store.take(f"{bucket}:{key}", limit_for(settings, bucket))
        if decision.allowed:
            return
        # INFO, not WARNING: a bucket doing its job is the system working, and at WARNING a
        # scripted burst would be the loudest thing in the log.
        log.info("rate_limited bucket=%s key=%s retry_after=%s", bucket, key, decision.retry_after)
        raise RateLimited(bucket=bucket, retry_after=decision.retry_after)

    return dependency
