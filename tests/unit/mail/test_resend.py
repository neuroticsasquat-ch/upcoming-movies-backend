"""`ResendClient` against a mocked transport — never the live network (`CLAUDE.md`)."""

import httpx
import pytest
import respx

from upmovies.llm.retry import RetryPolicy
from upmovies.mail import Envelope, MailError, ResendClient, Transport
from upmovies.mail.registry import RESEND_BASE_URL

EMAILS_URL = f"{RESEND_BASE_URL}/emails"

# Backoff is irrelevant to what these tests assert and waiting for it would make the suite
# slow for nothing. Attempt *counts* are still the real defaults.
_NO_WAIT = RetryPolicy(initial_backoff=0.0, jitter=0.0)

ENVELOPE = Envelope(
    sender="Backlotter <no-reply@example.com>",
    to="ada@example.com",
    subject="Confirm your email address",
    text="Follow https://example.com/verify",
    html="<p>Follow <a href='https://example.com/verify'>the link</a></p>",
)


def test_the_client_satisfies_the_transport_protocol():
    client: Transport = ResendClient(api_key="re_x")
    assert client is not None


@respx.mock
async def test_a_send_returns_the_providers_id_and_puts_both_bodies_on_the_wire():
    route = respx.post(EMAILS_URL).mock(
        return_value=httpx.Response(200, json={"id": "6229f547-0000-4a1f-bf1b-1f1f1f1f1f1f"})
    )

    async with ResendClient(api_key="re_x") as client:
        message_id = await client.send(ENVELOPE)

    assert message_id == "6229f547-0000-4a1f-bf1b-1f1f1f1f1f1f"
    body = route.calls.last.request
    assert body.headers["authorization"] == "Bearer re_x"
    import json

    sent = json.loads(body.content)
    assert sent == {
        "from": "Backlotter <no-reply@example.com>",
        "to": ["ada@example.com"],
        "subject": "Confirm your email address",
        "text": ENVELOPE.text,
        "html": ENVELOPE.html,
    }


@respx.mock
async def test_a_rate_limited_send_is_retried():
    """Resend rate-limits at a couple of requests a second and says so in `Retry-After`; the
    verdict comes from the same `retry_for_status` the LLM adapters use."""
    route = respx.post(EMAILS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": "after-the-retry"}),
        ]
    )

    async with ResendClient(api_key="re_x", policy=_NO_WAIT) as client:
        assert await client.send(ENVELOPE) == "after-the-retry"

    assert route.call_count == 2


@respx.mock
async def test_a_rejected_message_is_not_retried_and_raises():
    """422 is the provider saying the message is wrong — an unverified sending domain, a
    malformed `from`. Asking again buys the same refusal."""
    route = respx.post(EMAILS_URL).mock(
        return_value=httpx.Response(422, json={"message": "domain is not verified"})
    )

    async with ResendClient(api_key="re_x", policy=_NO_WAIT) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.send(ENVELOPE)

    assert route.call_count == 1


@respx.mock
async def test_a_server_error_exhausts_the_retries_then_raises():
    route = respx.post(EMAILS_URL).mock(return_value=httpx.Response(503))

    async with ResendClient(api_key="re_x", policy=_NO_WAIT) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.send(ENVELOPE)

    assert route.call_count == _NO_WAIT.max_retries + 1


@respx.mock
async def test_a_transport_error_is_retried():
    route = respx.post(EMAILS_URL).mock(
        side_effect=[
            httpx.ConnectError("no route to host"),
            httpx.Response(200, json={"id": "recovered"}),
        ]
    )

    async with ResendClient(api_key="re_x", policy=_NO_WAIT) as client:
        assert await client.send(ENVELOPE) == "recovered"

    assert route.call_count == 2


@respx.mock
async def test_an_accepted_message_with_no_id_is_refused_rather_than_returned_empty():
    """The id is the only record that the mail was accepted. An empty one looks like success
    everywhere it is later stored, and a lie does not get retried the way a failure does."""
    respx.post(EMAILS_URL).mock(return_value=httpx.Response(200, json={}))

    async with ResendClient(api_key="re_x", policy=_NO_WAIT) as client:
        with pytest.raises(MailError, match="no id"):
            await client.send(ENVELOPE)
