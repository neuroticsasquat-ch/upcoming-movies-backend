"""Email verification end to end (NEU-1339): signup mails a link, the link verifies once, and
a spent or expired one is refused.

The mailer is the autouse `mailbox` fixture (`tests/fixtures/mail.py`) — a real `MailGateway`
over a `NoopTransport`, so the template really renders and the assertions below are against
the message a user would receive."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from upmovies.app.models import EmailToken, User
from upmovies.app.services import verification_service
from upmovies.config import get_settings
from upmovies.mail import Envelope, MailGateway, MessageId
from upmovies.main import app


@pytest.fixture
async def client(session):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
    ) as c:
        yield c


async def _signup(client, make_invite, email: str = "new@example.com") -> None:
    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json={
            "email": email,
            "password": "hunter2hunter2",
            "display_name": "Newcomer",
            "turnstile_token": "solved",
            "invite_code": invite,
        },
    )
    assert r.status_code == 201
    assert r.json()["email_verified"] is False


def _token_in(envelope) -> str:
    """The token as the reader would get it: pulled out of the link in the plain-text part."""
    link = next(word for word in envelope.text.split() if "/verify?" in word)
    return parse_qs(urlparse(link).query)["token"][0]


def _token_from(mailbox) -> str:
    assert len(mailbox.sent) == 1
    return _token_in(mailbox.sent[0])


class _FailingTransport:
    """A provider having a bad minute: the httpx failure the Resend adapter would raise."""

    async def send(self, envelope: Envelope) -> MessageId:
        raise httpx.ConnectError("the mail provider is unreachable")

    async def aclose(self) -> None:
        pass


async def test_signup_mails_a_verification_link_that_verifies_the_account(
    client, session, make_invite, mailbox
):
    await _signup(client, make_invite)

    sent = mailbox.sent[0]
    assert sent.to == "new@example.com"
    assert sent.subject == "Confirm your email address"
    assert get_settings().public_base_url in sent.text

    r = await client.post("/auth/verify", json={"token": _token_from(mailbox)})
    assert r.status_code == 204
    assert r.content == b""  # the token holder is not handed the account record

    user = (
        await session.execute(
            select(User)
            .where(User.email == "new@example.com")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert user.email_verified_at is not None


async def test_a_token_verifies_once_and_is_then_refused(client, make_invite, mailbox):
    await _signup(client, make_invite)
    token = _token_from(mailbox)

    assert (await client.post("/auth/verify", json={"token": token})).status_code == 204

    second = await client.post("/auth/verify", json={"token": token})
    assert second.status_code == 400
    assert second.json()["detail"] == "invalid_token"


async def test_an_expired_token_is_refused(client, session, make_invite, mailbox):
    await _signup(client, make_invite)
    token = _token_from(mailbox)
    row = (await session.execute(select(EmailToken).where(EmailToken.token == token))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    r = await client.post("/auth/verify", json={"token": token})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_token"


async def test_an_unknown_token_is_refused(client):
    r = await client.post("/auth/verify", json={"token": "not-a-token"})
    assert r.status_code == 400


async def test_requesting_again_mails_a_fresh_link_without_killing_the_first(
    client, make_invite, mailbox
):
    await _signup(client, make_invite)
    first = _token_from(mailbox)

    r = await client.post("/auth/verify/request", json={"email": "new@example.com"})
    assert r.status_code == 202
    assert len(mailbox.sent) == 2
    second = _token_in(mailbox.sent[1])
    assert second != first

    # Both live. The route needs no session, so retiring the outstanding token on re-issue
    # would let anyone who knows the address invalidate the link in the victim's inbox, over
    # and over, while the mail tells her to ask for another one.
    assert (await client.post("/auth/verify", json={"token": first})).status_code == 204
    assert (await client.post("/auth/verify", json={"token": second})).status_code == 204


async def test_requesting_for_an_unknown_or_verified_address_says_the_same_thing_and_sends_nothing(
    client, make_invite, mailbox
):
    unknown = await client.post("/auth/verify/request", json={"email": "nobody@example.com"})
    assert unknown.status_code == 202
    assert mailbox.sent == []

    await _signup(client, make_invite)
    await client.post("/auth/verify", json={"token": _token_from(mailbox)})
    already = await client.post("/auth/verify/request", json={"email": "new@example.com"})
    assert already.status_code == 202
    assert len(mailbox.sent) == 1  # still just the signup mail


async def test_me_reports_verification_state(authed_client, session, mailbox):
    assert (await authed_client.get("/me")).json()["email_verified"] is False

    user = authed_client.user  # type: ignore[attr-defined]
    token = await verification_service.issue(session, user=user, settings=get_settings())
    r = await authed_client.post("/auth/verify", json={"token": token})
    assert r.status_code == 204

    assert (await authed_client.get("/me")).json()["email_verified"] is True


async def test_a_failing_provider_does_not_fail_the_signup(client, make_invite):
    """Verification gates outbound mail, not access (D-18). A provider having a bad minute
    must not answer a committed signup with a 500 — the account exists, the user is signed in,
    and `POST /auth/verify/request` is the recovery."""
    app.state.mailer = MailGateway(get_settings(), transport=_FailingTransport())

    invite = await make_invite()
    r = await client.post(
        "/auth/signup",
        json={
            "email": "unlucky@example.com",
            "password": "hunter2hunter2",
            "display_name": "Unlucky",
            "turnstile_token": "solved",
            "invite_code": invite,
        },
    )
    assert r.status_code == 201
    assert r.json()["email_verified"] is False
