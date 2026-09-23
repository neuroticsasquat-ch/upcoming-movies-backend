"""Changing an account's address end to end (NEU-1341): the address moves only after the
*new* inbox proves it holds it, and the old inbox is told while it can still do something
about it.

Same shape as `test_verify.py` and `test_reset.py` — the autouse `mailbox` fixture is a real
`MailGateway` over a `NoopTransport`, so the templates really render and the token below is
pulled out of the link exactly as a reader would get it."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.fixtures.users import _build_authed_client
from upmovies.app.models import EmailToken, User
from upmovies.app.services import verification_service
from upmovies.config import get_settings
from upmovies.mail import Envelope, MailGateway, MessageId
from upmovies.main import app

PASSWORD = "hunter2hunter2"
NEW_EMAIL = "new@example.com"


@pytest.fixture
async def client(session):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
    ) as c:
        yield c


class _FailingTransport:
    """A provider having a bad minute: the httpx failure the Resend adapter would raise."""

    async def send(self, envelope: Envelope) -> MessageId:
        raise httpx.ConnectError("the mail provider is unreachable")

    async def aclose(self) -> None:
        pass


class _FailsForTransport:
    """A provider that is unreachable for one recipient and fine for the rest — which is what
    makes the two sends in this flow observably independent."""

    def __init__(self, failing_recipient: str, delegate) -> None:
        self._failing = failing_recipient
        self._delegate = delegate

    async def send(self, envelope: Envelope) -> MessageId:
        if envelope.to == self._failing:
            raise httpx.ConnectError("the mail provider is unreachable")
        return await self._delegate.send(envelope)

    async def aclose(self) -> None:
        pass


def _token_in(envelope) -> str:
    """The token as the reader would get it: pulled out of the link in the plain-text part."""
    link = next(word for word in envelope.text.split() if "/email-change?" in word)
    return parse_qs(urlparse(link).query)["token"][0]


async def _request_change(
    authed_client, mailbox, *, new_email: str = NEW_EMAIL, password: str = PASSWORD
):
    return await authed_client.post(
        "/auth/email-change/request",
        json={"new_email": new_email, "current_password": password},
    )


async def _confirm_token(authed_client, mailbox, **kwargs) -> str:
    r = await _request_change(authed_client, mailbox, **kwargs)
    assert r.status_code == 202
    confirmation = next(e for e in mailbox.sent if e.to == kwargs.get("new_email", NEW_EMAIL))
    return _token_in(confirmation)


async def _reload(session, user_id) -> User:
    return (
        await session.execute(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_an_address_moves_only_after_the_new_one_confirms(
    authed_client, client, session, mailbox
):
    user = authed_client.user
    old_email = user.email

    token = await _confirm_token(authed_client, mailbox)

    # Requesting alone changes nothing: the whole point is that one side cannot move it.
    assert (await _reload(session, user.id)).email == old_email

    r = await client.post("/auth/email-change/confirm", json={"token": token})
    assert r.status_code == 204
    assert r.content == b""  # the token holder is not handed the account record

    changed = await _reload(session, user.id)
    assert changed.email == NEW_EMAIL
    assert changed.email_verified_at is not None  # confirming *is* proof of control

    assert (
        await client.post("/auth/login", json={"email": NEW_EMAIL, "password": PASSWORD})
    ).status_code == 200
    assert (
        await client.post("/auth/login", json={"email": old_email, "password": PASSWORD})
    ).status_code == 401


async def test_the_confirmation_goes_to_the_new_address_and_a_notice_to_the_old(
    authed_client, mailbox
):
    """Never trust a single-sided change: the link goes where the account is moving, and the
    address it is moving *from* hears about it at request time, while the move can still be
    stopped."""
    old_email = authed_client.user.email
    await _request_change(authed_client, mailbox)

    recipients = [e.to for e in mailbox.sent]
    assert sorted(recipients) == sorted([NEW_EMAIL, old_email])

    confirmation = next(e for e in mailbox.sent if e.to == NEW_EMAIL)
    notice = next(e for e in mailbox.sent if e.to == old_email)

    assert "/email-change?" in confirmation.text
    # The notice is a warning, not a second way to complete the change.
    assert "/email-change?" not in notice.text
    assert NEW_EMAIL in notice.text  # the owner has to see where it is going


async def test_the_wrong_password_will_not_start_a_change(authed_client, session, mailbox):
    r = await _request_change(authed_client, mailbox, password="not-the-password")
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"
    assert mailbox.sent == []
    assert (await _reload(session, authed_client.user.id)).email == "user@example.com"


async def test_an_address_another_account_already_holds_is_refused(
    authed_client, make_user, mailbox
):
    await make_user(email="taken@example.com")

    r = await _request_change(authed_client, mailbox, new_email="taken@example.com")
    assert r.status_code == 409
    assert r.json()["detail"] == "email_in_use"
    assert mailbox.sent == []


async def test_citext_uniqueness_is_respected_on_a_case_variant(authed_client, make_user, mailbox):
    """`app.user.email` is CITEXT, so `TAKEN@example.com` and `taken@example.com` are one
    address. A check that compared with `==` in Python would mint a token that could only fail
    at the constraint."""
    await make_user(email="taken@example.com")

    r = await _request_change(authed_client, mailbox, new_email="TAKEN@example.com")
    assert r.status_code == 409
    assert mailbox.sent == []


async def test_an_email_change_token_spends_once(authed_client, client, mailbox):
    token = await _confirm_token(authed_client, mailbox)

    assert (
        await client.post("/auth/email-change/confirm", json={"token": token})
    ).status_code == 204
    second = await client.post("/auth/email-change/confirm", json={"token": token})
    assert second.status_code == 400
    assert second.json()["detail"] == "invalid_token"


async def test_an_expired_email_change_token_is_refused(authed_client, client, session, mailbox):
    token = await _confirm_token(authed_client, mailbox)
    row = (await session.execute(select(EmailToken).where(EmailToken.token == token))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    r = await client.post("/auth/email-change/confirm", json={"token": token})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_token"


async def test_a_verification_token_cannot_be_spent_as_an_email_change(
    authed_client, client, session
):
    """`purpose` is what keeps the shared `email_token` table from being a cross-flow
    privilege escalation — and this token carries no address to move to in any case."""
    user = authed_client.user
    verify_token = await verification_service.issue(session, user=user, settings=get_settings())

    r = await client.post("/auth/email-change/confirm", json={"token": verify_token})
    assert r.status_code == 400
    assert (await _reload(session, user.id)).email == user.email


async def test_confirming_retires_the_other_live_change_links(
    authed_client, client, session, mailbox
):
    """A second live link would be a standing key that moves the account somewhere else, and
    the person who just changed their address can neither see nor revoke it."""
    first = await _confirm_token(authed_client, mailbox, new_email="first@example.com")
    second = await _confirm_token(authed_client, mailbox, new_email="second@example.com")
    assert first != second

    assert (
        await client.post("/auth/email-change/confirm", json={"token": second})
    ).status_code == 204
    stale = await client.post("/auth/email-change/confirm", json={"token": first})
    assert stale.status_code == 400
    assert (await _reload(session, authed_client.user.id)).email == "second@example.com"


async def test_an_address_taken_between_request_and_confirm_is_refused(
    authed_client, client, session, make_user, mailbox
):
    """The request-time check cannot be the last word: the address is free when the mail goes
    out and someone else may hold it by the time the link is opened."""
    token = await _confirm_token(authed_client, mailbox)
    await make_user(email=NEW_EMAIL, display_name="Whoever Got There First")

    r = await client.post("/auth/email-change/confirm", json={"token": token})
    assert r.status_code == 409
    assert r.json()["detail"] == "email_in_use"
    assert (await _reload(session, authed_client.user.id)).email == "user@example.com"


async def test_the_request_route_needs_a_session_and_csrf(client, authed_client, mailbox):
    """403 and not 401 for the anonymous caller: `require_csrf` is a route-level dependency, so
    it answers before the session is ever resolved — the same order `/auth/password` has. Both
    refusals are the point here; which one arrives first is not."""
    anonymous = await client.post(
        "/auth/email-change/request",
        json={"new_email": NEW_EMAIL, "current_password": PASSWORD},
    )
    assert anonymous.status_code == 403

    mismatched_csrf = await authed_client.post(
        "/auth/email-change/request",
        json={"new_email": NEW_EMAIL, "current_password": PASSWORD},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert mismatched_csrf.status_code == 403
    assert mailbox.sent == []


async def test_changing_to_the_address_the_account_already_has_is_refused(authed_client, mailbox):
    r = await _request_change(authed_client, mailbox, new_email=authed_client.user.email)
    assert r.status_code == 409
    assert mailbox.sent == []


async def test_the_change_leaves_the_signed_in_session_alone(authed_client, client, mailbox):
    """Unlike a password change, which drops every session: this flow is finished from the new
    inbox, on a device that may never have signed in, and there is nothing about proving
    control of an address that says the browser holding the session is not the owner's."""
    token = await _confirm_token(authed_client, mailbox)
    assert (
        await client.post("/auth/email-change/confirm", json={"token": token})
    ).status_code == 204

    me = await authed_client.get("/me")
    assert me.status_code == 200
    assert me.json()["email"] == NEW_EMAIL
    assert me.json()["email_verified"] is True


async def test_the_mails_name_the_product_and_the_window(authed_client, mailbox):
    await _request_change(authed_client, mailbox)

    settings = get_settings()
    confirmation = next(e for e in mailbox.sent if e.to == NEW_EMAIL)
    assert settings.product_name in confirmation.text
    assert str(settings.email_change_token_ttl_hours) in confirmation.text
    assert confirmation.subject
    assert confirmation.html

    notice = next(e for e in mailbox.sent if e.to == authed_client.user.email)
    assert settings.product_name in notice.text
    assert notice.subject
    assert notice.html


async def test_changing_the_password_revokes_a_pending_change(authed_client, client, mailbox):
    """What the notice to the old address promises. Without it the advice in that mail is
    useless: a token already sitting in the requester's inbox would outlive the password it
    was authorised with, and the owner would have no way to stop the move."""
    token = await _confirm_token(authed_client, mailbox)

    changed = await authed_client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "a whole new password"},
    )
    assert changed.status_code == 200

    stale = await client.post("/auth/email-change/confirm", json={"token": token})
    assert stale.status_code == 400
    assert stale.json()["detail"] == "invalid_token"


async def test_a_password_reset_revokes_a_pending_change(authed_client, client, session, mailbox):
    """The same guarantee reached the other way, and the case it matters most in: a reset is
    what someone does when they think the account is already compromised, and a live change
    link is exactly how an attacker who got in first would keep it."""
    change_token = await _confirm_token(authed_client, mailbox)

    assert (
        await client.post("/auth/reset/request", json={"email": authed_client.user.email})
    ).status_code == 202
    reset_link = next(word for word in mailbox.sent[-1].text.split() if "/reset?" in word)
    reset_token = parse_qs(urlparse(reset_link).query)["token"][0]
    assert (
        await client.post(
            "/auth/reset", json={"token": reset_token, "new_password": "a whole new password"}
        )
    ).status_code == 204

    stale = await client.post("/auth/email-change/confirm", json={"token": change_token})
    assert stale.status_code == 400
    assert (await _reload(session, authed_client.user.id)).email == "user@example.com"


async def test_a_failing_provider_still_accepts_the_request_and_leaves_the_link_live(
    authed_client, client, session
):
    """The send is best-effort, as in both sibling flows: the token row is already committed
    when the mail goes out, so raising would answer a request that did happen with a 500 and
    leave a live link behind a caller who was told it failed."""
    app.state.mailer = MailGateway(get_settings(), transport=_FailingTransport())

    r = await _request_change(authed_client, None)
    assert r.status_code == 202

    # The token exists and is spendable, which is what "the request did happen" means.
    row = (
        await session.execute(select(EmailToken).where(EmailToken.purpose == "email_change"))
    ).scalar_one()
    assert row.new_email == NEW_EMAIL
    assert (
        await client.post("/auth/email-change/confirm", json={"token": row.token})
    ).status_code == 204


async def test_one_failed_send_does_not_suppress_the_other(authed_client, mailbox):
    """The notice is mailed first, deliberately: the two sends fail independently, and the
    warning to the old address is the half that must survive a bad minute on the other."""
    app.state.mailer = MailGateway(get_settings(), transport=_FailsForTransport(NEW_EMAIL, mailbox))

    r = await _request_change(authed_client, mailbox)
    assert r.status_code == 202

    # The confirmation was refused by the provider; the warning still went out.
    assert [e.to for e in mailbox.sent] == [authed_client.user.email]


async def test_confirm_respects_citext_when_the_address_is_taken_after_the_request(
    authed_client, client, session, make_user, mailbox
):
    """The request-time case-insensitivity has a confirm-time twin: the token carries
    `new@example.com` and a signup takes `NEW@Example.com` before the link is opened."""
    token = await _confirm_token(authed_client, mailbox)
    await make_user(email="NEW@Example.com")

    r = await client.post("/auth/email-change/confirm", json={"token": token})
    assert r.status_code == 409
    assert (await _reload(session, authed_client.user.id)).email == "user@example.com"


async def test_a_conflict_at_confirm_leaves_the_token_spendable(
    authed_client, client, session, make_user, mailbox
):
    """`confirm` refuses without consuming, on purpose: burning the token would make a race
    somebody else won cost the user the whole flow rather than one retry."""
    token = await _confirm_token(authed_client, mailbox)
    squatter = await make_user(email=NEW_EMAIL)

    assert (
        await client.post("/auth/email-change/confirm", json={"token": token})
    ).status_code == 409

    # The address frees up, and the same link still works.
    await session.delete(squatter)
    await session.commit()
    assert (
        await client.post("/auth/email-change/confirm", json={"token": token})
    ).status_code == 204
    assert (await _reload(session, authed_client.user.id)).email == NEW_EMAIL


async def test_an_email_change_token_cannot_be_spent_as_a_verification(
    authed_client, client, session, mailbox
):
    """The reverse of the cross-purpose test above. Spending it here would stamp the *old*
    address verified off a token that only ever proved control of the new one."""
    token = await _confirm_token(authed_client, mailbox)

    r = await client.post("/auth/verify", json={"token": token})
    assert r.status_code == 400

    refreshed = await _reload(session, authed_client.user.id)
    assert refreshed.email == "user@example.com"
    assert refreshed.email_verified_at is None


async def test_retiring_a_pending_change_does_not_touch_another_account(
    authed_client, client, session, make_user, mailbox
):
    """`retire_pending` filters on `user_id`: one user changing their password must not
    cancel somebody else's pending move."""
    token = await _confirm_token(authed_client, mailbox)

    other = await make_user(email="other@example.com")
    other_client = await _build_authed_client(session, other)
    async with other_client:
        assert (
            await other_client.post(
                "/auth/password",
                json={"current_password": PASSWORD, "new_password": "a whole new password"},
            )
        ).status_code == 200

    assert (
        await client.post("/auth/email-change/confirm", json={"token": token})
    ).status_code == 204


async def test_confirming_leaves_the_users_verify_and_reset_links_alone(
    authed_client, client, session, mailbox
):
    """`retire_pending` is scoped to one purpose. Finishing an address change says nothing
    about a reset link the same person asked for, and retiring it would make the two mails in
    an inbox invalidate each other in arrival order."""
    reset_request = await client.post(
        "/auth/reset/request", json={"email": authed_client.user.email}
    )
    assert reset_request.status_code == 202
    reset_link = next(word for word in mailbox.sent[-1].text.split() if "/reset?" in word)
    reset_token = parse_qs(urlparse(reset_link).query)["token"][0]

    token = await _confirm_token(authed_client, mailbox)
    assert (
        await client.post("/auth/email-change/confirm", json={"token": token})
    ).status_code == 204

    assert (
        await client.post(
            "/auth/reset", json={"token": reset_token, "new_password": "a whole new password"}
        )
    ).status_code == 204


async def test_the_move_clears_a_lockout_standing_against_the_new_address(
    authed_client, client, make_user, mailbox
):
    """`authenticate` counts failures by address and records them even for addresses with no
    account, so without this the owner's own typos at the sign-in form transfer onto the
    account the moment it moves — and refuse them the password they still hold."""
    token = await _confirm_token(authed_client, mailbox)
    for _ in range(5):
        await client.post("/auth/login", json={"email": NEW_EMAIL, "password": "wrong-password"})

    assert (
        await client.post("/auth/email-change/confirm", json={"token": token})
    ).status_code == 204

    fresh = await client.post("/auth/login", json={"email": NEW_EMAIL, "password": PASSWORD})
    assert fresh.status_code == 200


async def test_confirming_opens_no_session_of_its_own(client, authed_client, mailbox):
    """It is reached without one and hands none back: the token proves control of an address,
    which is not a reason to sign its holder in as the account."""
    token = await _confirm_token(authed_client, mailbox)

    r = await client.post("/auth/email-change/confirm", json={"token": token})
    assert r.status_code == 204
    assert r.headers.get_list("set-cookie") == []
