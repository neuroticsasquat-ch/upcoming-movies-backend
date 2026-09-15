"""`NoopTransport` — the provider that sends nothing and remembers everything."""

from upmovies.mail import NoopTransport, Transport
from upmovies.mail.types import Envelope

ENVELOPE = Envelope(
    sender="no-reply@example.com",
    to="ada@example.com",
    subject="Confirm your email address",
    text="text",
    html="<p>html</p>",
)


def test_it_satisfies_the_transport_protocol():
    transport: Transport = NoopTransport()
    assert transport is not None


async def test_it_records_every_envelope_in_order():
    transport = NoopTransport()

    await transport.send(ENVELOPE)
    await transport.send(Envelope(**{**ENVELOPE.__dict__, "to": "grace@example.com"}))

    assert [e.to for e in transport.sent] == ["ada@example.com", "grace@example.com"]
    assert transport.sent[0].subject == "Confirm your email address"


async def test_each_send_gets_a_distinct_id():
    """Callers store the id (D-31's notification rows); a constant would make two sends
    indistinguishable in the one place that records they happened."""
    transport = NoopTransport()

    first = await transport.send(ENVELOPE)
    second = await transport.send(ENVELOPE)

    assert first != second
    assert first.startswith("noop-")


async def test_a_send_is_logged_so_a_forgotten_MAIL_PROVIDER_is_visible(caplog):
    """The cost of `noop` being the safe default is a production container that sends
    nothing; the log line is what keeps that visible in the log stream."""
    import logging

    transport = NoopTransport()
    with caplog.at_level(logging.INFO, logger="upmovies.mail.noop"):
        await transport.send(ENVELOPE)

    assert "ada@example.com" in caplog.text
    assert "MAIL_PROVIDER=noop" in caplog.text


async def test_closing_is_a_no_op():
    await NoopTransport().aclose()
