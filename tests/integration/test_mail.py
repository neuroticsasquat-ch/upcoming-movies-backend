"""The `verify` mail end to end: the real template tree, the real gateway, the real Resend
wire format — with respx standing in for the provider (`CLAUDE.md`: never the live network).

The unit tests each hold one piece still. This one holds none of them: a change that renames a
template part, drops a context key from the template, or reshapes the Resend body fails here
and nowhere else."""

import json

import httpx
import pytest
import respx

from upmovies.config import get_settings
from upmovies.mail import Envelope, MailGateway, NoopTransport
from upmovies.mail.registry import RESEND_BASE_URL

EMAILS_URL = f"{RESEND_BASE_URL}/emails"

VERIFY_URL = "https://app.upmovies.localhost/verify?token=tok_abc123"
CONTEXT: dict[str, object] = {
    "display_name": "Ada",
    "product_name": "Backlotter",
    "verify_url": VERIFY_URL,
    "expires_in_hours": 24,
}


def _resend_settings(**overrides: object):
    return get_settings().model_copy(
        update={
            "mail_provider": "resend",
            "mail_from": "Backlotter <no-reply@upmovies.test>",
            "resend_api_key": "re_integration",
            **overrides,
        }
    )


@respx.mock
async def test_the_verify_mail_renders_and_sends_through_resend():
    route = respx.post(EMAILS_URL).mock(
        return_value=httpx.Response(200, json={"id": "1b2c3d4e-0000-4000-8000-000000000001"})
    )

    async with MailGateway(_resend_settings()) as mail:
        message_id = await mail.send(to="ada@example.com", template="verify", context=CONTEXT)

    assert message_id == "1b2c3d4e-0000-4000-8000-000000000001"

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer re_integration"
    sent = json.loads(request.content)
    assert sent["from"] == "Backlotter <no-reply@upmovies.test>"
    assert sent["to"] == ["ada@example.com"]
    assert sent["subject"] == "Confirm your email address"
    # Both parts, both carrying the link: the text part is the one that has to stand alone.
    assert VERIFY_URL in sent["text"]
    assert VERIFY_URL in sent["html"]
    assert "Ada" in sent["text"]
    assert "24 hours" in sent["text"]


@respx.mock
async def test_the_gateway_closes_its_connection_pool_on_exit():
    """The gateway owns the pool the app's lifespan hands it; a leaked one outlives the
    process's reason to hold it."""
    respx.post(EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "re-1"}))

    mail = MailGateway(_resend_settings())
    async with mail:
        await mail.send(to="ada@example.com", template="verify", context=CONTEXT)
    client = mail._transport

    assert client is not None
    with pytest.raises(RuntimeError, match="closed"):
        await client.send(
            Envelope(sender="a@b.co", to="c@d.co", subject="s", text="t", html="<p>h</p>")
        )


async def test_local_dev_sends_the_same_message_without_a_resend_account():
    """`MAIL_PROVIDER=noop` is the default, and the point of it is that this is the *same*
    rendered message — the template tree is not a resend-only path."""
    transport = NoopTransport()
    settings = get_settings().model_copy(
        update={"mail_provider": "noop", "mail_from": "no-reply@upmovies.test"}
    )

    async with MailGateway(settings, transport=transport) as mail:
        await mail.send(to="ada@example.com", template="verify", context=CONTEXT)

    envelope = transport.sent[0]
    assert envelope.subject == "Confirm your email address"
    assert VERIFY_URL in envelope.text
    assert VERIFY_URL in envelope.html
