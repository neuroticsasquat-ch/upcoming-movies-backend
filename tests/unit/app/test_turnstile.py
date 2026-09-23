"""`CloudflareTurnstile` against a mocked transport — never the live network (`CLAUDE.md`)."""

import logging

import httpx
import pytest
import respx

from upmovies.app.turnstile import (
    BYPASS_SECRET,
    SITEVERIFY_URL,
    BypassVerifier,
    CloudflareTurnstile,
    TurnstileUnavailable,
    Verifier,
    verifier_for,
)
from upmovies.llm.retry import RetryPolicy

# Backoff is irrelevant to what these assert and waiting for it would make the suite slow for
# nothing. Attempt *counts* are still the real defaults.
_NO_WAIT = RetryPolicy(timeout=5.0, max_retries=2, initial_backoff=0.0, jitter=0.0)


def test_the_client_satisfies_the_verifier_protocol():
    client: Verifier = CloudflareTurnstile(secret="secret")
    assert client is not None


@respx.mock
async def test_a_solved_challenge_verifies_and_sends_the_secret_in_the_body():
    """And sends *only* the secret and the token: `remoteip` is left out until the service has
    a real client address to put in it (NEU-1344 turns `--proxy-headers` on), because
    Cloudflare scores the token against whatever address it is given and the one available
    today is Coolify's proxy."""
    route = respx.post(SITEVERIFY_URL).mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    verified = await CloudflareTurnstile(secret="0x-secret").verify("tok")

    assert verified is True
    request = route.calls.last.request
    assert dict(httpx.QueryParams(request.content.decode())) == {
        "secret": "0x-secret",
        "response": "tok",
    }


@respx.mock
async def test_a_rejected_token_is_a_false_verdict_not_an_error():
    respx.post(SITEVERIFY_URL).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error-codes": ["invalid-input-response"]}
        )
    )

    assert await CloudflareTurnstile(secret="0x-secret").verify("stale") is False


@respx.mock
async def test_cloudflares_own_internal_error_is_unavailable_rather_than_a_rejection():
    """It arrives as a 200 with `success: false`, which is also how a bad token arrives. Read
    as a rejection it would tell the user to solve a widget that was never the problem."""
    respx.post(SITEVERIFY_URL).mock(
        return_value=httpx.Response(200, json={"success": False, "error-codes": ["internal-error"]})
    )

    with pytest.raises(TurnstileUnavailable):
        await CloudflareTurnstile(secret="0x-secret").verify("tok")


@respx.mock
async def test_a_transient_failure_is_retried():
    route = respx.post(SITEVERIFY_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"success": True}),
        ]
    )

    assert await CloudflareTurnstile(secret="0x-secret", policy=_NO_WAIT).verify("tok") is True
    assert route.call_count == 2


@respx.mock
async def test_an_unreachable_provider_raises_rather_than_passing_the_signup():
    """The whole direction of the gate: no verdict means no signup, not a free one."""
    respx.post(SITEVERIFY_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(TurnstileUnavailable):
        await CloudflareTurnstile(secret="0x-secret", policy=_NO_WAIT).verify("tok")


@respx.mock
async def test_a_body_that_does_not_parse_is_unavailable_too():
    respx.post(SITEVERIFY_URL).mock(return_value=httpx.Response(200, content=b"<html>nope</html>"))

    with pytest.raises(TurnstileUnavailable):
        await CloudflareTurnstile(secret="0x-secret", policy=_NO_WAIT).verify("tok")


@respx.mock
async def test_the_bypass_verifier_calls_nobody():
    route = respx.post(SITEVERIFY_URL)

    assert await BypassVerifier().verify("anything") is True
    assert route.call_count == 0


def test_the_configured_secret_picks_the_verifier():
    assert isinstance(verifier_for("0x-real-secret"), CloudflareTurnstile)
    assert isinstance(verifier_for(BYPASS_SECRET), BypassVerifier)


def test_the_bypass_says_so_in_the_log_every_time_it_is_selected(caplog):
    """The bypass is a documented value one uncommented line away in `.env.example`, so the
    only thing standing between it and production is somebody noticing. A deployment running
    without a bot check says so in the log stream, at WARNING, the way `mail.noop` says it is
    transmitting nothing."""
    with caplog.at_level(logging.WARNING, logger="upmovies.app.turnstile"):
        verifier_for(BYPASS_SECRET)

    assert any(
        record.levelno == logging.WARNING and "without being verified" in record.getMessage()
        for record in caplog.records
    )


def test_a_real_secret_logs_no_such_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="upmovies.app.turnstile"):
        verifier_for("0x-real-secret")

    assert caplog.records == []


def test_an_unset_secret_configures_no_verifier_at_all():
    """`None` rather than something permissive: an unconfigured gate is the deployment's
    problem to fix, and `deps.get_turnstile` refuses the signup until it is."""
    assert verifier_for("") is None
