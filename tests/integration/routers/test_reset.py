"""Password reset end to end (NEU-1340): a forgotten password is recoverable from the inbox,
the link spends once, and everything the old password could reach dies with it.

Same shape as `test_verify.py` — the autouse `mailbox` fixture is a real `MailGateway` over a
`NoopTransport`, so the template really renders and the token below is pulled out of the link
exactly as a reader would get it."""

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

OLD_PASSWORD = "hunter2hunter2"
NEW_PASSWORD = "correct horse battery"


@pytest.fixture
async def client(session):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
    ) as c:
        yield c


def _token_in(envelope) -> str:
    """The token as the reader would get it: pulled out of the link in the plain-text part."""
    link = next(word for word in envelope.text.split() if "/reset?" in word)
    return parse_qs(urlparse(link).query)["token"][0]


async def _request_reset(client, mailbox, email: str = "user@example.com") -> str:
    r = await client.post("/auth/reset/request", json={"email": email})
    assert r.status_code == 202
    return _token_in(mailbox.sent[-1])


class _FailingTransport:
    """A provider having a bad minute: the httpx failure the Resend adapter would raise."""

    async def send(self, envelope: Envelope) -> MessageId:
        raise httpx.ConnectError("the mail provider is unreachable")

    async def aclose(self) -> None:
        pass


async def test_a_forgotten_password_is_recoverable_end_to_end(client, make_user, mailbox):
    user = await make_user()

    token = await _request_reset(client, mailbox)
    sent = mailbox.sent[0]
    assert sent.to == user.email
    assert get_settings().public_base_url in sent.text

    r = await client.post("/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})
    assert r.status_code == 204
    assert r.content == b""  # the token holder is not handed the account record

    stale = await client.post("/auth/login", json={"email": user.email, "password": OLD_PASSWORD})
    assert stale.status_code == 401

    fresh = await client.post("/auth/login", json={"email": user.email, "password": NEW_PASSWORD})
    assert fresh.status_code == 200


async def test_reset_kills_every_session_the_old_password_opened(
    authed_client, client, session, mailbox
):
    """The point of the route: whoever knew the old password is signed out everywhere, not
    merely unable to sign in again."""
    assert (await authed_client.get("/me")).status_code == 200

    token = await _request_reset(client, mailbox)
    r = await client.post("/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})
    assert r.status_code == 204

    assert (await authed_client.get("/me")).status_code == 401


async def test_an_unknown_address_leaks_nothing(client, make_user, mailbox):
    """Same status, same body, no mail — the route takes a bare address and needs no session,
    so any difference between these two answers is an account-enumeration oracle."""
    unknown = await client.post("/auth/reset/request", json={"email": "nobody@example.com"})
    assert unknown.status_code == 202
    assert unknown.content == b""
    assert mailbox.sent == []

    await make_user()
    known = await client.post("/auth/reset/request", json={"email": "user@example.com"})
    assert known.status_code == 202
    assert known.content == b""
    assert (known.status_code, known.content) == (unknown.status_code, unknown.content)
    assert len(mailbox.sent) == 1


async def test_a_reset_token_spends_once(client, make_user, mailbox):
    await make_user()
    token = await _request_reset(client, mailbox)

    first = await client.post("/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})
    assert first.status_code == 204

    second = await client.post(
        "/auth/reset", json={"token": token, "new_password": "another password"}
    )
    assert second.status_code == 400
    assert second.json()["detail"] == "invalid_token"


async def test_an_expired_reset_token_is_refused(client, session, make_user, mailbox):
    await make_user()
    token = await _request_reset(client, mailbox)
    row = (await session.execute(select(EmailToken).where(EmailToken.token == token))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    r = await client.post("/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_token"


async def test_an_unknown_reset_token_is_refused(client):
    r = await client.post(
        "/auth/reset", json={"token": "not-a-token", "new_password": NEW_PASSWORD}
    )
    assert r.status_code == 400


async def test_requesting_twice_leaves_both_links_live_until_one_is_spent(
    client, make_user, mailbox
):
    """Issuing does not retire the outstanding token, for the reason `/auth/verify/request`
    does not either: the route needs no session, so supersession on issue would let anyone who
    knows the address invalidate the link sitting in the victim's inbox, repeatedly."""
    await make_user()
    first = await _request_reset(client, mailbox)
    second = await _request_reset(client, mailbox)
    assert len(mailbox.sent) == 2
    assert first != second

    # ...but spending one retires the other: after a reset, a second live reset link is a
    # standing key to the account that the person who just reset it cannot see or revoke.
    assert (
        await client.post("/auth/reset", json={"token": second, "new_password": NEW_PASSWORD})
    ).status_code == 204
    stale = await client.post(
        "/auth/reset", json={"token": first, "new_password": "yet another password"}
    )
    assert stale.status_code == 400


async def test_a_verification_token_cannot_be_spent_as_a_reset(client, session, make_user):
    """`purpose` is what keeps the shared `email_token` table from being a cross-flow
    privilege escalation: a link that proves an address must not also set a password."""
    user = await make_user()
    verify_token = await verification_service.issue(session, user=user, settings=get_settings())

    r = await client.post("/auth/reset", json={"token": verify_token, "new_password": NEW_PASSWORD})
    assert r.status_code == 400

    assert (
        await client.post("/auth/login", json={"email": user.email, "password": OLD_PASSWORD})
    ).status_code == 200


async def test_a_reset_will_not_set_a_password_too_short_to_have_been_allowed_at_signup(
    client, make_user, mailbox
):
    await make_user()
    token = await _request_reset(client, mailbox)

    r = await client.post("/auth/reset", json={"token": token, "new_password": "short"})
    assert r.status_code == 422

    # And the rejected attempt did not burn the token.
    assert (
        await client.post("/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})
    ).status_code == 204


async def test_a_failing_provider_still_answers_the_same_202(client, make_user):
    """The route cannot report a send failure without also reporting that the address exists."""
    app.state.mailer = MailGateway(get_settings(), transport=_FailingTransport())
    await make_user()

    r = await client.post("/auth/reset/request", json={"email": "user@example.com"})
    assert r.status_code == 202


async def test_the_reset_mail_names_the_product_and_the_window(client, make_user, mailbox):
    await make_user()
    await _request_reset(client, mailbox)

    settings = get_settings()
    sent = mailbox.sent[0]
    assert settings.product_name in sent.text
    assert str(settings.reset_token_ttl_hours) in sent.text
    assert sent.subject
    assert sent.html


async def test_reset_does_not_disturb_another_users_sessions(
    authed_client, client, make_user, mailbox
):
    other = await make_user(email="other@example.com")
    token = await _request_reset(client, mailbox, email=other.email)

    assert (
        await client.post("/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})
    ).status_code == 204
    assert (await authed_client.get("/me")).status_code == 200


async def test_the_user_row_keeps_its_verification_state_across_a_reset(
    client, session, make_user, mailbox
):
    """A reset changes the password and nothing else about the account."""
    user = await make_user()
    token = await _request_reset(client, mailbox)
    await client.post("/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})

    refreshed = (
        await session.execute(
            select(User).where(User.id == user.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.email_verified_at is None
    assert refreshed.display_name == user.display_name


async def test_a_reset_clears_the_lockout_that_sent_the_user_to_it(client, make_user, mailbox):
    """The flow's most common entry: you reset *because* you just failed login several times.
    `authenticate` checks the lockout before the password, so a reset that left those rows
    behind would refuse the password it had just set, for up to the lockout window."""
    user = await make_user()
    for _ in range(5):
        await client.post("/auth/login", json={"email": user.email, "password": "wrong-password"})
    locked = await client.post("/auth/login", json={"email": user.email, "password": OLD_PASSWORD})
    assert locked.status_code == 401  # correct password, still locked out

    token = await _request_reset(client, mailbox)
    assert (
        await client.post("/auth/reset", json={"token": token, "new_password": NEW_PASSWORD})
    ).status_code == 204

    fresh = await client.post("/auth/login", json={"email": user.email, "password": NEW_PASSWORD})
    assert fresh.status_code == 200


async def test_resetting_another_account_from_a_signed_in_browser_leaves_that_session_alone(
    authed_client, make_user, mailbox
):
    """The reset route is reached without a session, so the cookie on the request says nothing
    about the account in the token: clearing cookies here would sign this browser's owner out
    of *her own* live session because a link for a different account was opened in it.

    Asserted on the response headers rather than on a follow-up request, because
    `authed_client` re-injects its cookies on every call and would paper over a `Set-Cookie`
    that told a real browser to drop them."""
    other = await make_user(email="other@example.com")
    r = await authed_client.post("/auth/reset/request", json={"email": other.email})
    assert r.status_code == 202
    token = _token_in(mailbox.sent[-1])

    reset = await authed_client.post(
        "/auth/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert reset.status_code == 204
    assert reset.headers.get_list("set-cookie") == []

    # Her session row is untouched too — only the token's owner was signed out.
    assert (await authed_client.get("/me")).status_code == 200
