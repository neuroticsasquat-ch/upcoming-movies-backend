"""`ResendClient` against a mocked transport — never the live network (`CLAUDE.md`)."""

from dataclasses import replace

import httpx
import pytest
import respx

from upmovies.llm.retry import RetryPolicy
from upmovies.mail import (
    DEFAULT_MAIL_RETRY_POLICY,
    Envelope,
    MailError,
    ResendClient,
    Transport,
)
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
async def test_envelope_headers_go_on_the_wire_as_resends_headers_object():
    """DC-10: Resend's `POST /emails` takes custom message headers as a `headers` object —
    the one-click unsubscribe pair must arrive there verbatim, not as HTTP request headers."""
    import json

    route = respx.post(EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "re-1"}))
    headers = {
        "List-Unsubscribe": "<https://api.example.com/digest/unsubscribe/tok>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }

    async with ResendClient(api_key="re_x") as client:
        await client.send(replace(ENVELOPE, headers=headers))

    sent = json.loads(route.calls.last.request.content)
    assert sent["headers"] == headers
    assert "list-unsubscribe" not in route.calls.last.request.headers


@respx.mock
async def test_an_envelope_without_headers_sends_no_headers_key():
    """A transactional mail's wire body is exactly what it was before the field existed."""
    import json

    route = respx.post(EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "re-1"}))

    async with ResendClient(api_key="re_x") as client:
        await client.send(ENVELOPE)

    assert "headers" not in json.loads(route.calls.last.request.content)


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


# --- retrying a side-effecting endpoint ----------------------------------------


@respx.mock
async def test_the_retries_of_one_send_carry_one_idempotency_key():
    """The LLM adapters' retry reasoning does not carry over: on a read timeout or a 5xx the
    request was already on the wire, so Resend may have accepted and queued the message. One
    key per *logical* send is what stops four attempts becoming four verification mails; a key
    per attempt would be no key at all."""
    route = respx.post(EMAILS_URL).mock(
        side_effect=[
            httpx.ReadTimeout("no answer"),
            httpx.Response(500),
            httpx.Response(200, json={"id": "accepted-once"}),
        ]
    )

    async with ResendClient(api_key="re_x", policy=_NO_WAIT) as client:
        assert await client.send(ENVELOPE) == "accepted-once"

    keys = {call.request.headers["idempotency-key"] for call in route.calls}
    assert route.call_count == 3
    assert len(keys) == 1


@respx.mock
async def test_two_separate_sends_carry_different_idempotency_keys():
    """The key deduplicates one send's retries, not two deliberate sends of the same mail —
    a user who asks for a second verification link must get one."""
    route = respx.post(EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "re-1"}))

    async with ResendClient(api_key="re_x", policy=_NO_WAIT) as client:
        await client.send(ENVELOPE)
        await client.send(ENVELOPE)

    keys = {call.request.headers["idempotency-key"] for call in route.calls}
    assert len(keys) == 2


def test_the_mail_timeout_is_short_enough_to_sit_inside_a_request():
    """`DEFAULT_RETRY_POLICY`'s 60s reproduces the Anthropic SDK's configuration — sensible
    for a model generating tokens, and over three minutes of a held signup request once the
    retries are counted. The retry *semantics* stay shared; only the ceiling moves."""
    assert DEFAULT_MAIL_RETRY_POLICY.timeout == 10.0
    worst_case = DEFAULT_MAIL_RETRY_POLICY.timeout * (DEFAULT_MAIL_RETRY_POLICY.max_retries + 1)
    assert worst_case <= 45.0


def test_the_client_defaults_to_the_mail_policy():
    assert ResendClient(api_key="re_x")._policy is DEFAULT_MAIL_RETRY_POLICY
