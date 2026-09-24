"""`MailGateway` — provider resolution, the lifecycle over the transport it builds, and the
boot check that refuses a configuration it cannot send with."""

import httpx
import pytest
import respx

from upmovies.config import get_settings
from upmovies.mail import (
    Envelope,
    MailConfigurationError,
    Mailer,
    MailGateway,
    MissingCredentialError,
    NoopTransport,
    ResendClient,
    TemplateRenderError,
    credential_for,
    validate_mail_configuration,
)
from upmovies.mail.registry import RESEND_BASE_URL

EMAILS_URL = f"{RESEND_BASE_URL}/emails"

VERIFY_CONTEXT: dict[str, object] = {
    "display_name": "Ada",
    "product_name": "Backlotter",
    "verify_url": "https://app.example.com/verify?token=abc123",
    "expires_in_hours": 24,
}

RESEND_CONFIG: dict[str, object] = {
    "mail_provider": "resend",
    "mail_from": "Backlotter <no-reply@example.com>",
    "resend_api_key": "re_test",
}

NOOP_CONFIG: dict[str, object] = {
    "mail_provider": "noop",
    "mail_from": "",
    "resend_api_key": None,
}


def settings_with(**overrides: object):
    return get_settings().model_copy(update=overrides)


def test_the_gateway_satisfies_the_mailer_protocol():
    mailer: Mailer = MailGateway(settings_with(**NOOP_CONFIG))
    assert mailer is not None


# --- provider resolution -------------------------------------------------------


async def test_noop_resolves_to_the_recording_transport_and_renders_the_template():
    async with MailGateway(settings_with(**NOOP_CONFIG)) as mail:
        message_id = await mail.send(
            to="ada@example.com", template="verify", context=VERIFY_CONTEXT
        )
        transport = mail._resolve()

    assert mail.provider == "noop"
    assert message_id.startswith("noop-")
    assert isinstance(transport, NoopTransport)
    assert transport.sent[0].to == "ada@example.com"
    assert "abc123" in transport.sent[0].text


@respx.mock
async def test_resend_resolves_to_the_http_client_and_sends_once():
    route = respx.post(EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "re-1"}))

    async with MailGateway(settings_with(**RESEND_CONFIG)) as mail:
        assert isinstance(mail._resolve(), ResendClient)
        assert await mail.send(to="ada@example.com", template="verify", context=VERIFY_CONTEXT)

    assert route.call_count == 1


@respx.mock
async def test_the_transport_is_built_once_and_pooled_across_sends():
    """A process that sends a lot of mail should hold one connection pool, and one that sends
    none should hold no pool at all."""
    respx.post(EMAILS_URL).mock(return_value=httpx.Response(200, json={"id": "re-1"}))

    async with MailGateway(settings_with(**RESEND_CONFIG)) as mail:
        assert mail._transport is None
        await mail.send(to="a@example.com", template="verify", context=VERIFY_CONTEXT)
        first = mail._transport
        await mail.send(to="b@example.com", template="verify", context=VERIFY_CONTEXT)
        assert mail._transport is first


async def test_a_gateway_that_never_sends_builds_no_transport():
    async with MailGateway(settings_with(**RESEND_CONFIG)) as mail:
        pass
    assert mail._transport is None


async def test_resend_without_a_credential_refuses_rather_than_falling_back_to_noop():
    """There is no fallback provider. Falling back to `noop` would turn a credential typo into
    mail that is never sent and never reported."""
    mail = MailGateway(settings_with(**{**RESEND_CONFIG, "resend_api_key": None}))
    with pytest.raises(MissingCredentialError, match="RESEND_API_KEY"):
        async with mail:
            await mail.send(to="a@example.com", template="verify", context=VERIFY_CONTEXT)


async def test_an_injected_transport_wins_over_the_configured_provider_and_is_not_closed():
    """The seam tests and local dev come in through. Whoever built the transport owns closing
    it — a test asserting on `sent` after the block has exited must still be able to."""
    transport = NoopTransport()
    async with MailGateway(settings_with(**RESEND_CONFIG), transport=transport) as mail:
        await mail.send(to="ada@example.com", template="verify", context=VERIFY_CONTEXT)

    assert transport.sent[0].to == "ada@example.com"


async def test_a_template_fault_costs_no_provider_call():
    """Rendering happens before the transport is touched, so a bad template cannot half-send."""
    transport = NoopTransport()
    async with MailGateway(settings_with(**NOOP_CONFIG), transport=transport) as mail:
        with pytest.raises(TemplateRenderError):
            await mail.send(to="ada@example.com", template="verify", context={})

    assert transport.sent == []


async def test_sending_through_a_closed_gateway_is_refused():
    mail = MailGateway(settings_with(**NOOP_CONFIG))
    async with mail:
        pass
    with pytest.raises(RuntimeError, match="closed"):
        await mail.send(to="a@example.com", template="verify", context=VERIFY_CONTEXT)


async def test_deliver_hands_the_envelope_to_the_transport_as_it_stands():
    """The digest's way in (D-1460.1): the caller rendered it, so the transport gets exactly
    that value — no second render, nothing re-read from settings."""
    envelope = Envelope(
        sender="Someone Else <x@example.com>",
        to="ada@example.com",
        subject="Already rendered",
        text="Plain.",
        html="<p>Plain.</p>",
    )
    transport = NoopTransport()
    async with MailGateway(settings_with(**NOOP_CONFIG), transport=transport) as mail:
        await mail.deliver(envelope)

    assert transport.sent == [envelope]


async def test_delivering_through_a_closed_gateway_is_refused():
    transport = NoopTransport()
    mail = MailGateway(settings_with(**NOOP_CONFIG), transport=transport)
    async with mail:
        pass
    envelope = Envelope(sender="s@example.com", to="a@example.com", subject="S", text="T", html="")
    with pytest.raises(RuntimeError, match="closed"):
        await mail.deliver(envelope)
    assert transport.sent == []


async def test_the_gateway_names_the_provider_a_message_id_should_be_read_against():
    assert MailGateway(settings_with(**RESEND_CONFIG)).provider == "resend"


# --- credentials ---------------------------------------------------------------


def test_an_empty_credential_counts_as_unset():
    """The compose file passes `RESEND_API_KEY: "${RESEND_API_KEY:-}"`, so an unconfigured
    credential arrives as "" rather than as an absent variable."""
    assert credential_for(settings_with(resend_api_key=""), "resend") is None
    assert credential_for(settings_with(resend_api_key="re_x"), "resend") == "re_x"


def test_noop_has_no_credential_to_be_missing():
    assert credential_for(settings_with(**NOOP_CONFIG), "noop") is None


def test_an_unknown_provider_raises_rather_than_guessing_a_credential():
    with pytest.raises(KeyError):
        credential_for(settings_with(**NOOP_CONFIG), "postmark")


# --- the boot check ------------------------------------------------------------


def test_the_default_noop_configuration_boots():
    """Every deploy that exists today has no Resend account; none of their boots may break."""
    validate_mail_configuration(settings_with(**NOOP_CONFIG))


def test_a_complete_resend_configuration_boots():
    validate_mail_configuration(settings_with(**RESEND_CONFIG))


def test_resend_without_a_key_fails_the_boot():
    with pytest.raises(MailConfigurationError, match="RESEND_API_KEY is unset"):
        validate_mail_configuration(settings_with(**{**RESEND_CONFIG, "resend_api_key": None}))


def test_resend_without_a_deliverable_sender_fails_the_boot():
    """Catches `MAIL_FROM=Backlotter`, which Resend rejects at send time with a 422 nobody
    sees until a user signs up."""
    with pytest.raises(MailConfigurationError, match="MAIL_FROM"):
        validate_mail_configuration(settings_with(**{**RESEND_CONFIG, "mail_from": "Backlotter"}))


def test_the_bare_and_named_sender_forms_are_both_accepted():
    validate_mail_configuration(settings_with(**{**RESEND_CONFIG, "mail_from": "a@b.co"}))
    validate_mail_configuration(settings_with(**{**RESEND_CONFIG, "mail_from": "A <a@b.co>"}))


def test_every_fault_is_reported_from_one_failed_boot():
    """A deploy turning mail on for the first time should learn about its missing key *and*
    its missing sender at once, not one boot at a time."""
    with pytest.raises(MailConfigurationError) as exc:
        validate_mail_configuration(
            settings_with(**{**RESEND_CONFIG, "resend_api_key": "", "mail_from": ""})
        )

    assert "RESEND_API_KEY" in str(exc.value)
    assert "MAIL_FROM" in str(exc.value)


def test_an_unknown_provider_fails_the_boot_even_though_the_literal_should_have_caught_it():
    """Unreachable through `Settings`; the backstop for a settings object assembled outside an
    entrypoint — a script, or a test."""
    with pytest.raises(MailConfigurationError, match="MAIL_PROVIDER"):
        validate_mail_configuration(settings_with(mail_provider="postmark"))


def test_a_missing_template_tree_fails_the_boot(monkeypatch, tmp_path):
    """The packaging guard: the templates are data files nothing in the import graph
    references, so a build that drops them imports and starts perfectly."""
    from upmovies.mail import templates as templates_module

    monkeypatch.setattr(templates_module, "_TEMPLATE_ROOT", tmp_path / "gone")

    with pytest.raises(MailConfigurationError, match="does not exist"):
        validate_mail_configuration(settings_with(**NOOP_CONFIG))


async def test_an_unimplemented_provider_gets_no_transport_rather_than_a_resend_one():
    """Unreachable while the `Literal` guards `Settings`, and stated anyway: a third provider
    added to the registry must not fall through to a Resend client. Sending someone else's
    mail through Resend is a worse failure than refusing to build."""
    mail = MailGateway(settings_with(mail_provider="postmark", resend_api_key="re_x"))
    with pytest.raises(MailConfigurationError, match="postmark"):
        async with mail:
            await mail.send(to="a@example.com", template="verify", context=VERIFY_CONTEXT)
