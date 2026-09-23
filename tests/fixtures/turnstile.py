"""A Turnstile verifier for every test that reaches a route, without a site key or the network.

Autouse for the reason the `mailbox` fixture beside it is: `POST /auth/signup` verifies a
challenge before it writes anything (NEU-1343), so without this every signup test in the suite
would get `deps.get_turnstile`'s `turnstile_unconfigured` 503 — CI sets no `TURNSTILE_SECRET`,
and unset deliberately means "refuse", not "allow".

Overriding the dependency rather than setting the bypass secret keeps the *rejection* path
reachable: a test flips `turnstile.verdict` to False, or sets `turnstile.failure`, and gets the
403 and the 503 a real Cloudflare answer would produce. The adapter that talks to Cloudflare is
covered on its own in `tests/unit/app/test_turnstile.py`, against respx.
"""

from collections.abc import Iterator

import pytest

from upmovies.deps import get_turnstile
from upmovies.main import app


class StubVerifier:
    """Answers whatever the test asked for, and records what it was asked about."""

    def __init__(self) -> None:
        self.verdict = True
        self.failure: Exception | None = None
        self.seen: list[str] = []

    async def verify(self, token: str) -> bool:
        self.seen.append(token)
        if self.failure is not None:
            raise self.failure
        return self.verdict


@pytest.fixture(autouse=True)
def turnstile() -> Iterator[StubVerifier]:
    stub = StubVerifier()
    app.dependency_overrides[get_turnstile] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_turnstile, None)
