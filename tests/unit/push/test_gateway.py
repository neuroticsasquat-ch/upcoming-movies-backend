"""The VAPID configuration check and the one call that puts a push on the wire (D-36).

`pywebpush` is patched out here rather than driven against a fake push service: what this
module actually owns is the translation — which refusals mean "this endpoint is gone", which
mean "this send failed", and what is handed to the library — and the encryption in between is
the library's business, not ours."""

import pytest
from pywebpush import WebPushException

from upmovies.config import Settings
from upmovies.push import (
    PUSH_TTL_SECONDS,
    PushConfigurationError,
    PushError,
    PushSubscriptionGone,
    PushSubscriptionInfo,
    WebPushGateway,
    push_configuration_problems,
    validate_push_configuration,
)

SUBSCRIPTION = PushSubscriptionInfo(
    endpoint="https://push.example.test/fcm/abc", p256dh="p256dh-value", auth="auth-value"
)


def _settings(**overrides) -> Settings:
    values = {
        "VAPID_PUBLIC_KEY": "a-public-key",
        "VAPID_PRIVATE_KEY": "a-private-key",
        "VAPID_SUBJECT": "mailto:ops@example.test",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[call-arg]


class _Response:
    """A push service's answer, in the shape `WebPushException` reads a status off."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = f"status {status_code}"


# --- configuration ---------------------------------------------------------------------------


def test_a_complete_configuration_has_no_problems():
    assert push_configuration_problems(_settings()) == []


def test_every_missing_variable_is_reported_together():
    """One failed boot, not three: a deploy turning push on for the first time should learn
    about all of it at once."""
    problems = push_configuration_problems(
        _settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="", VAPID_SUBJECT="")
    )
    assert len(problems) == 3
    assert {p.split()[0] for p in problems} == {
        "VAPID_PUBLIC_KEY",
        "VAPID_PRIVATE_KEY",
        "VAPID_SUBJECT",
    }


def test_a_subject_that_is_not_a_url_is_a_problem():
    """`VAPID_SUBJECT=ops@example.test` looks right and is refused on the wire."""
    (problem,) = push_configuration_problems(_settings(VAPID_SUBJECT="ops@example.test"))
    assert "VAPID_SUBJECT" in problem


@pytest.mark.parametrize("subject", ["mailto:ops@example.test", "https://example.test/contact"])
def test_both_documented_subject_forms_are_accepted(subject):
    assert push_configuration_problems(_settings(VAPID_SUBJECT=subject)) == []


def test_validate_raises_naming_every_fault():
    with pytest.raises(PushConfigurationError) as excinfo:
        validate_push_configuration(_settings(VAPID_PRIVATE_KEY="", VAPID_SUBJECT=""))
    message = str(excinfo.value)
    assert "VAPID_PRIVATE_KEY" in message
    assert "VAPID_SUBJECT" in message


def test_validate_is_silent_on_a_complete_configuration():
    validate_push_configuration(_settings())


# --- sending ---------------------------------------------------------------------------------


async def test_the_library_is_handed_the_subscription_payload_and_ttl(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("upmovies.push.gateway.webpush", lambda **kwargs: calls.append(kwargs))

    await WebPushGateway(_settings()).send(subscription=SUBSCRIPTION, payload='{"title":"Dune"}')

    (call,) = calls
    assert call["subscription_info"] == {
        "endpoint": SUBSCRIPTION.endpoint,
        "keys": {"p256dh": "p256dh-value", "auth": "auth-value"},
    }
    assert call["data"] == '{"title":"Dune"}'
    assert call["vapid_private_key"] == "a-private-key"
    assert call["vapid_claims"] == {"sub": "mailto:ops@example.test"}
    assert call["ttl"] == PUSH_TTL_SECONDS


async def test_each_send_gets_its_own_claims_dict(monkeypatch):
    """`pywebpush` mutates the claims it is given — it fills in `aud` from the endpoint — so a
    shared dict would carry the first push service's audience to the second, which rejects it.
    The failure only appears once two services are involved, which is to say in production."""

    def _mutating(**kwargs):
        kwargs["vapid_claims"]["aud"] = "https://push.example.test"

    monkeypatch.setattr("upmovies.push.gateway.webpush", _mutating)
    gateway = WebPushGateway(_settings())

    await gateway.send(subscription=SUBSCRIPTION, payload="{}")
    seen: list[dict] = []
    monkeypatch.setattr(
        "upmovies.push.gateway.webpush", lambda **kwargs: seen.append(kwargs["vapid_claims"])
    )
    await gateway.send(subscription=SUBSCRIPTION, payload="{}")

    assert seen == [{"sub": "mailto:ops@example.test"}]


@pytest.mark.parametrize("status_code", [404, 410])
async def test_a_gone_endpoint_raises_subscription_gone(monkeypatch, status_code):
    """The two RFC 8030 spellings of "this endpoint is not a thing any more" — the only
    refusal with a side effect, since the sender deletes the row."""

    def _raise(**kwargs):
        raise WebPushException("gone", response=_Response(status_code))

    monkeypatch.setattr("upmovies.push.gateway.webpush", _raise)

    with pytest.raises(PushSubscriptionGone):
        await WebPushGateway(_settings()).send(subscription=SUBSCRIPTION, payload="{}")


@pytest.mark.parametrize("status_code", [400, 429, 500])
async def test_any_other_refusal_is_a_plain_push_error(monkeypatch, status_code):
    """A statement about this minute, not about the registration — so the subscription
    survives it."""

    def _raise(**kwargs):
        raise WebPushException("refused", response=_Response(status_code))

    monkeypatch.setattr("upmovies.push.gateway.webpush", _raise)

    with pytest.raises(PushError) as excinfo:
        await WebPushGateway(_settings()).send(subscription=SUBSCRIPTION, payload="{}")
    assert not isinstance(excinfo.value, PushSubscriptionGone)


async def test_a_transport_failure_is_a_push_error_too(monkeypatch):
    """Everything `requests` raises from inside the worker thread, plus the `ValueError`
    `py_vapid` raises on a malformed key. The send pass's contract is that a refusal is a
    failed row, not an exception it has to know the shape of."""

    def _raise(**kwargs):
        raise ConnectionError("connection reset")

    monkeypatch.setattr("upmovies.push.gateway.webpush", _raise)

    with pytest.raises(PushError) as excinfo:
        await WebPushGateway(_settings()).send(subscription=SUBSCRIPTION, payload="{}")
    assert "ConnectionError" in str(excinfo.value)
