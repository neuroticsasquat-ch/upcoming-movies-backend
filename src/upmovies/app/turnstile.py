"""Cloudflare Turnstile verification: the check standing where the invite code used to (D-18).

An invite code was a secret this service issued and could settle against its own table. A
Turnstile token is a claim about the browser that only Cloudflare can settle, so the gate is
now an outbound call, and every failure mode of one comes with it. Two of them are decided
here rather than at the route, because the route cannot tell them apart from what it is
handed: a provider that cannot be reached **refuses** the signup (`CloudflareTurnstile.verify`
raises rather than returning False), and a deployment with no secret configured has no gate at
all, which `verifier_for` reports as `None` instead of quietly waving signups through.

httpx over the vendor SDK is ADR-0007's call, the same one `mail/resend.py` makes, and the
retry loop is `llm.retry` for the reason that module's docstring gives. What is deliberately
*not* borrowed from the mail package is its gateway: one verification per signup, with no
template tree and no provider choice in front of it, does not earn a lazily-built process-wide
pool and the lifespan wiring to close it.
"""

import logging
from typing import Any, Protocol

import httpx

from upmovies.llm.retry import (
    Attempts,
    Retry,
    RetryPolicy,
    call_with_retry,
    retry_for_status,
)

log = logging.getLogger(__name__)

# Cloudflare's server-side endpoint. A constant rather than a setting for the reason
# `mail.registry`'s base URL is one: it is a property of the provider, not of a deployment,
# and the tests reach the adapter by mocking the transport rather than by pointing it
# somewhere else.
SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# The one `TURNSTILE_SECRET` value that means "do not call Cloudflare" (see `BypassVerifier`).
BYPASS_SECRET = "dev-bypass"

# The shared retry semantics on a timeout a signup can live with — `DEFAULT_RETRY_POLICY`'s
# 60s is sized for a model generating tokens, and this call sits inside a request a person is
# waiting on. Two retries rather than three for the same reason: past a few seconds the user
# has given up and pressed the button again, and a second signup attempt re-solves the widget
# anyway.
DEFAULT_TURNSTILE_RETRY_POLICY = RetryPolicy(timeout=5.0, max_retries=2)

# Cloudflare's own "this one is on us, ask again" verdict. It arrives as a 200 with
# `success: false`, which is otherwise how a *rejected* token arrives — so without this set a
# provider-side fault would be reported to the user as a failed challenge, which is the one
# thing they cannot fix by solving the widget again.
_UNAVAILABLE_ERROR_CODES = frozenset({"internal-error"})


class TurnstileUnavailable(RuntimeError):
    """The verdict could not be obtained: the provider was unreachable, answered with a status
    the retry loop gave up on, or returned a body that did not parse.

    Distinct from a token being *rejected*, which is an ordinary `False`. The caller owes the
    two different answers — the rejected token is the client's to fix by solving the challenge
    again, this one is not — and merging them would tell a user to retry a widget that was
    never the problem."""


class Verifier(Protocol):
    """What the signup route needs: a token goes in, a verdict comes out.

    A Protocol for the reason `mail.types.Mailer` is one — the route depends on the surface,
    not on the httpx-owning implementation, so a test hands it a stub without the application's
    configuration coming along."""

    async def verify(self, token: str) -> bool: ...


def _classify(exc: BaseException) -> Retry | None:
    """Which httpx failures are worth another attempt — the judgement `mail.resend` makes,
    deferred to the same `retry_for_status` so it is literally the same one.

    A 4xx outside that set is Cloudflare saying the *request* is wrong (a malformed secret,
    most likely). Asking again buys the same refusal."""
    if isinstance(exc, httpx.HTTPStatusError):
        return retry_for_status(exc.response.status_code, exc.response.headers)
    if isinstance(exc, httpx.TransportError):
        # Connect errors, read errors and timeouts: the request never got an answer, so
        # nothing is known about whether it would fail again.
        return Retry()
    return None


class CloudflareTurnstile:
    """Verifies tokens against Cloudflare's siteverify endpoint.

    Built with its secret rather than with `Settings`, so it is constructible in a test
    without the application's configuration — `verifier_for` is the piece that knows how to
    get one from the other, exactly as `MailGateway` does for the Resend client."""

    def __init__(
        self,
        *,
        secret: str,
        policy: RetryPolicy = DEFAULT_TURNSTILE_RETRY_POLICY,
        url: str = SITEVERIFY_URL,
    ) -> None:
        self._secret = secret
        self._policy = policy
        self._url = url

    async def verify(self, token: str) -> bool:
        """Ask Cloudflare whether `token` is a solved challenge.

        **Raises rather than returning False when the answer cannot be had.** The signup route
        turns that into a 503 and creates no account, which is the deliberate direction: this
        call is the only thing standing between open signup and a script, so an outage that
        degraded to "allow" would be an outage that quietly opened the gate — and nobody would
        notice, because the successful signups look exactly like the legitimate ones. Refusing
        is visible, recoverable by retrying, and bounded by how long Cloudflare is down.

        **`remoteip` is deliberately not sent yet.** Cloudflare takes the visitor's address as
        an optional third field and scores the token against it, so sending the *wrong* one is
        worse than sending none — and today the only address this service has is the wrong
        one: the prod entrypoint runs uvicorn without `--proxy-headers` (`Dockerfile`), so
        `request.client.host` behind Coolify's proxy is the proxy. The flag arrives with the
        rate limiter (D-19, NEU-1344), which needs a real client address for its buckets and
        for the SSR Worker's forwarded-IP header (NEU-1389); this is worth adding back on top
        of that, with a test, and not before."""
        form = {"secret": self._secret, "response": token}
        try:
            payload = await call_with_retry(
                lambda: self._post(form),
                policy=self._policy,
                classify=_classify,
                attempts=Attempts(),
            )
        except (httpx.HTTPError, ValueError) as err:
            # `ValueError` is the JSON decoder's: a 2xx whose body is not the documented
            # object tells us nothing about the token, so it is the same "no verdict" as a
            # connection that never landed.
            raise TurnstileUnavailable(f"could not reach Turnstile at {self._url}: {err}") from err
        if payload.get("success") is True:
            return True
        codes = payload.get("error-codes") or []
        if any(code in _UNAVAILABLE_ERROR_CODES for code in codes):
            raise TurnstileUnavailable(f"Turnstile reported an internal error: {codes}")
        return False

    async def _post(self, form: dict[str, str]) -> dict[str, Any]:
        """One attempt. The client is per-call rather than pooled on the instance because the
        verifier itself is per-request (`deps.get_turnstile`), so a pool here would be built
        and dropped just as often while adding a lifecycle nobody closes."""
        async with httpx.AsyncClient(timeout=self._policy.timeout) as client:
            response = await client.post(self._url, data=form)
            response.raise_for_status()
            return response.json()


class BypassVerifier:
    """Accepts any token without calling Cloudflare — what `TURNSTILE_SECRET=dev-bypass` buys.

    For local development and the test suite, which have no site key to solve a widget against
    and are forbidden the live network anyway (`CLAUDE.md`). It is a *named* value rather than
    an empty-secret fallback so that turning the gate off is something a deployment has to say
    out loud: an unset secret is `verifier_for`'s `None`, which refuses signups outright, and
    only this exact string opens them."""

    async def verify(self, token: str) -> bool:
        return True


def verifier_for(secret: str) -> Verifier | None:
    """The verifier `TURNSTILE_SECRET` selects, or `None` when it is unset.

    Empty counts as unset for the reason it does in `mail.gateway.credential_for`: compose
    passes `TURNSTILE_SECRET: "${TURNSTILE_SECRET:-}"`, so an unconfigured deployment arrives
    as `""` rather than as an absent variable.

    `None` rather than a verifier that refuses everything, because "no gate is configured" and
    "this token is bad" are different answers and only the caller can say them: one is a 503
    the operator has to fix, the other a 403 the user can. It is also why this returns nothing
    permissive — an unconfigured secret is the state a first deploy is in before the Coolify
    variable is set (spec §7), and signup being closed for those few minutes is the safe half
    of that window."""
    if not secret:
        return None
    if secret == BYPASS_SECRET:
        # Said out loud, every time one is built. The bypass is a documented value sitting
        # one uncommented line away in `.env.example`, so the argument above — unset refuses,
        # only this string opens — holds exactly as long as somebody would *notice* it being
        # set. This is `mail.noop`'s answer to the same problem (`config.MAIL_PROVIDER`):
        # a capability that is deliberately doing nothing says so in the log stream rather
        # than only in a support ticket. WARNING rather than INFO because, unlike unsent mail,
        # the thing not happening here is the bot check.
        log.warning(
            "TURNSTILE_SECRET is the bypass value: signup challenges are accepted without "
            "being verified against Cloudflare. Expected in local development and the test "
            "suite; never in production."
        )
        return BypassVerifier()
    return CloudflareTurnstile(secret=secret)
