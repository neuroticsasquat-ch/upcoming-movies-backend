"""The rate limiter as the routes see it (NEU-1344).

The suite runs with `RATE_LIMIT_ENABLED=false` (tests/conftest.py), so every test here turns
it back on for itself by overriding `get_settings`, and overrides the store as well so one
test's burst cannot be another's 429. The clock is injected for the reason the unit tests
inject one: the `signup` bucket refills at 0.083 tokens a minute.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from upmovies.app.rate_limit import (
    CLIENT_IP_HEADER,
    ORIGIN_HEADER,
    MemoryStore,
    client_ip,
    get_rate_limit_store,
)
from upmovies.config import Settings, get_settings
from upmovies.main import app

SIGNUP_BODY = {
    "email": "burst@example.com",
    "password": "hunter2hunter2",
    "display_name": "Burst",
    "turnstile_token": "solved",
}


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock) -> MemoryStore:
    """One store for the test, not one per request: the dependency override is called on every
    call, so a lambda that *built* a store would hand each request an untouched set of
    buckets — and nothing would ever be refused."""
    return MemoryStore(clock=clock)


@pytest.fixture
def limited(store) -> Iterator[Settings]:
    """Turn the limiter on with small, fast buckets, against a store of this test's own.

    The rest of `Settings` still comes from the environment, so the app is otherwise the one
    every other route test runs against."""
    settings = Settings(  # type: ignore[call-arg]
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_PUBLIC_ENABLED="false",
        RATE_LIMIT_SIGNUP="2/1",
        RATE_LIMIT_LOGIN="2/1",
        RATE_LIMIT_AUTH_REQUEST="2/1",
        RATE_LIMIT_PUBLIC="3/60",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_rate_limit_store] = lambda: store
    yield settings
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_rate_limit_store, None)


@pytest.fixture
async def client(session):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
    ) as c:
        yield c


def _signed(settings: Settings, ip: str) -> dict[str, str]:
    assert settings.ssr_origin_secret is not None
    return {ORIGIN_HEADER: settings.ssr_origin_secret, CLIENT_IP_HEADER: ip}


# --- the 429 itself ------------------------------------------------------------------


async def test_a_burst_past_capacity_is_refused_with_retry_after(client, limited):
    for _ in range(2):
        r = await client.post("/auth/reset/request", json={"email": "a@example.com"})
        assert r.status_code == 202
    r = await client.post("/auth/reset/request", json={"email": "a@example.com"})
    assert r.status_code == 429
    assert r.json() == {"detail": "rate_limited", "bucket": "auth_request", "retry_after": 60}
    assert r.headers["retry-after"] == "60"


async def test_the_bucket_refills_and_lets_the_next_request_through(client, limited, clock):
    for _ in range(3):
        await client.post("/auth/reset/request", json={"email": "a@example.com"})
    clock.advance(60)
    r = await client.post("/auth/reset/request", json={"email": "a@example.com"})
    assert r.status_code == 202


async def test_buckets_do_not_share_their_tokens(client, limited):
    """`/auth/reset/request` and `/auth/verify/request` share the `auth_request` bucket;
    `/auth/login` has its own. Exhausting one must not touch the other."""
    for _ in range(3):
        await client.post("/auth/reset/request", json={"email": "a@example.com"})
    r = await client.post("/auth/login", json={"email": "a@example.com", "password": "nope"})
    assert r.status_code == 401  # reached the route, which refused the credentials


async def test_routes_sharing_a_bucket_share_its_tokens(client, limited):
    await client.post("/auth/reset/request", json={"email": "a@example.com"})
    await client.post("/auth/verify/request", json={"email": "a@example.com"})
    r = await client.post("/auth/verify/request", json={"email": "a@example.com"})
    assert r.status_code == 429
    assert r.json()["bucket"] == "auth_request"


async def test_signup_is_limited_before_anything_is_written(client, limited):
    for i in range(2):
        r = await client.post("/auth/signup", json={**SIGNUP_BODY, "email": f"b{i}@example.com"})
        assert r.status_code == 201
    r = await client.post("/auth/signup", json={**SIGNUP_BODY, "email": "b2@example.com"})
    assert r.status_code == 429
    assert r.json()["bucket"] == "signup"


async def test_login_is_limited_by_its_own_bucket(client, limited, make_user):
    """The `login` bucket meters *requests from one host*, whatever address they are for —
    which is the half the per-email lockout cannot see."""
    await make_user(email="real@example.com", password="hunter2hunter2")
    for i in range(2):
        r = await client.post(
            "/auth/login", json={"email": f"other{i}@example.com", "password": "wrong-one"}
        )
        assert r.status_code == 401
    r = await client.post(
        "/auth/login", json={"email": "real@example.com", "password": "hunter2hunter2"}
    )
    assert r.status_code == 429
    assert r.json()["bucket"] == "login"


async def test_the_login_lockout_still_works_underneath_the_bucket(client, make_user):
    """The two limits are independent and both still apply (spec AC). With the bucket wide
    enough not to interfere, the fifth failure for one address locks that address out — and
    the lockout is what refuses the *correct* password afterwards, with the same 401 it always
    gave, so this route still tells an attacker nothing."""
    settings = Settings(  # type: ignore[call-arg]
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_LOGIN="100/100",
        LOGIN_LOCKOUT_THRESHOLD="5",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        await make_user(email="locked@example.com", password="hunter2hunter2")
        for _ in range(5):
            r = await client.post(
                "/auth/login", json={"email": "locked@example.com", "password": "wrong-one"}
            )
            assert r.status_code == 401
        r = await client.post(
            "/auth/login", json={"email": "locked@example.com", "password": "hunter2hunter2"}
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid_credentials"
    finally:
        app.dependency_overrides.pop(get_settings, None)


async def test_an_unlimited_route_is_untouched_by_a_neighbour_s_burst(client, limited):
    for _ in range(3):
        await client.post("/auth/reset/request", json={"email": "a@example.com"})
    assert (await client.get("/healthz")).status_code == 200


# --- the master switch and the public gate -------------------------------------------


async def test_the_limiter_is_off_when_disabled(client):
    """`RATE_LIMIT_ENABLED=false` is the suite's own default, and what this asserts is that
    the routes really are unmetered under it — otherwise every other test in the suite is
    resting on an assumption nothing checks."""
    for _ in range(8):
        r = await client.post("/auth/reset/request", json={"email": "a@example.com"})
        assert r.status_code == 202


async def test_public_routes_are_not_limited_by_default(client, limited):
    """Spec §5: the `public` bucket ships wired but inert, so this deploys before the Worker
    signs its requests without throttling the whole server-rendered site through a handful of
    Cloudflare egress IPs."""
    for _ in range(10):
        assert (await client.get("/feed")).status_code == 200


async def test_public_routes_are_limited_once_the_flag_is_set(client, limited):
    settings = Settings(  # type: ignore[call-arg]
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_PUBLIC_ENABLED="true",
        RATE_LIMIT_PUBLIC="3/60",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    for _ in range(3):
        assert (await client.get("/feed")).status_code == 200
    r = await client.get("/feed")
    assert r.status_code == 429
    assert r.json()["bucket"] == "public"


async def test_the_public_routes_share_one_bucket(client, store):
    settings = Settings(  # type: ignore[call-arg]
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_PUBLIC_ENABLED="true",
        RATE_LIMIT_PUBLIC="4/60",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_rate_limit_store] = lambda: store
    try:
        assert (await client.get("/feed")).status_code == 200
        assert (await client.get("/feed/grouped")).status_code == 200
        assert (await client.get("/calendar")).status_code == 200
        assert (await client.get("/films/search?q=dune")).status_code == 200
        # The sitemap is deliberately outside the bucket — crawlers, cached upstream — so it
        # answers after the other four have spent every token.
        assert (await client.get("/films/0-nothing")).status_code == 429
        assert (await client.get("/sitemap.xml")).status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_rate_limit_store, None)


# --- worker-signed requests ----------------------------------------------------------


@pytest.fixture
def signed(store) -> Iterator[Settings]:
    settings = Settings(  # type: ignore[call-arg]
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_PUBLIC_ENABLED="true",
        RATE_LIMIT_PUBLIC="2/60",
        SSR_ORIGIN_SECRET="worker-secret",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_rate_limit_store] = lambda: store
    yield settings
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_rate_limit_store, None)


async def test_a_signed_request_is_metered_against_the_visitor_it_names(client, signed):
    for _ in range(2):
        r = await client.get("/feed", headers=_signed(signed, "203.0.113.7"))
        assert r.status_code == 200
    assert (await client.get("/feed", headers=_signed(signed, "203.0.113.7"))).status_code == 429
    # A different visitor behind the same Worker is a different bucket — which is the whole
    # point of the header: without it every server-rendered page shares one egress IP.
    assert (await client.get("/feed", headers=_signed(signed, "203.0.113.8"))).status_code == 200


async def test_an_unsigned_request_keys_on_its_socket_address(client, signed):
    """The two callers are separate: exhausting the Worker's visitor does not touch the
    browser talking to the API directly."""
    for _ in range(2):
        await client.get("/feed", headers=_signed(signed, "203.0.113.7"))
    assert (await client.get("/feed")).status_code == 200


async def test_a_forged_signature_is_ignored_rather_than_rejected(client, signed):
    """A wrong secret does not fail the request — it is simply not believed, and the caller is
    metered on the address the socket says. Refusing would turn a stale Worker secret into an
    outage."""
    forged = {ORIGIN_HEADER: "not-the-secret", CLIENT_IP_HEADER: "203.0.113.7"}
    for _ in range(2):
        assert (await client.get("/feed", headers=forged)).status_code == 200
    # The third is refused on the *socket* key, not the one the header claimed...
    assert (await client.get("/feed", headers=forged)).status_code == 429
    # ...which the genuinely signed request for that same claimed visitor proves, by passing.
    assert (await client.get("/feed", headers=_signed(signed, "203.0.113.7"))).status_code == 200


# --- proxy headers -------------------------------------------------------------------


async def test_client_ip_honours_x_forwarded_for_through_the_proxy_middleware():
    """What `--proxy-headers --forwarded-allow-ips='*'` buys, asserted against the very
    middleware uvicorn installs when it is passed those flags (`Dockerfile`).

    The application itself never reads `X-Forwarded-For`: by the time a request reaches
    `client_ip` the middleware has already rewritten `scope["client"]`, which is exactly why
    the limiter keys on `request.client.host` and looks at no forwarded header of its own.
    Run against a one-route probe rather than the real app so what is under test is the
    header handling and nothing else."""
    settings = Settings()  # type: ignore[call-arg]
    probe = FastAPI()

    @probe.get("/probe")
    async def _probe(request: Request) -> dict[str, str]:
        return {"ip": client_ip(request, settings)}

    # `cast` on both sides because uvicorn's middleware is typed against uvicorn's own ASGI
    # protocol types and Starlette and httpx against theirs; they are the same protocol,
    # spelled three times, and nothing here is actually ambiguous at runtime.
    wrapped = cast(Any, ProxyHeadersMiddleware(cast(Any, probe), trusted_hosts="*"))
    async with AsyncClient(transport=ASGITransport(app=wrapped), base_url="https://test") as c:
        r = await c.get("/probe", headers={"X-Forwarded-For": "198.51.100.23"})
    assert r.json() == {"ip": "198.51.100.23"}


async def test_both_dockerfile_cmds_pass_the_proxy_header_flags():
    """The other half of the proxy-header acceptance criterion: the behaviour above is worth
    nothing if the flags that install that middleware are dropped from the image.

    Read as text rather than by building the image — what is being asserted is that the flags
    are in both CMDs, which is exactly what a careless edit to either one would remove."""
    cmds = [
        line
        for line in Path("Dockerfile").read_text().splitlines()
        if line.startswith("CMD ") and "uvicorn" in line
    ]
    assert len(cmds) == 2, "expected one uvicorn CMD for each of the dev and prod targets"
    for cmd in cmds:
        assert "--proxy-headers" in cmd
        assert "--forwarded-allow-ips" in cmd
