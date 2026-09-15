"""Issuing, mailing and consuming email-verification tokens.

Verification is the first of M1's three mailed-token flows (reset and email change follow it),
and the shape it sets is: the token row is committed *before* the mail is handed to the
provider, and a provider failure is logged rather than raised. Both halves of that matter.

* **Row first.** A mail carrying a token that is not in the database yet is a link that fails
  for whoever clicks it fastest. The reverse order costs nothing: an unspent token nobody was
  ever mailed expires on its own.
* **A failed send does not fail the caller.** At signup the user row and the session are
  already committed by the time the mail goes out, so raising would answer a successful
  signup with a 500 and leave the caller with an account they were told they did not get. The
  recovery for "no mail arrived" is the same in every case — request another one — so the
  send is best-effort and loud in the logs. Note this is *not* covering for a
  misconfiguration: `validate_mail_configuration` refuses to boot a container that cannot
  send at all (D-30), so what reaches here is a provider having a bad minute."""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import InvalidToken
from upmovies.app.models import User
from upmovies.app.repos import email_token_repo, user_repo
from upmovies.app.tokens import new_email_token
from upmovies.app.verification import is_verified
from upmovies.config import Settings
from upmovies.mail import (
    MailConfigurationError,
    Mailer,
    MailError,
    MessageId,
    MissingCredentialError,
)

logger = logging.getLogger(__name__)

# The `purpose` these tokens carry in `app.email_token`, and the name of the mail template
# that sends them. They are the same string because the flow is one thing; the constant exists
# so a typo in either place is an import error rather than a token nobody can spend.
VERIFY = "verify"


def verify_url(token: str, settings: Settings) -> str:
    """The link that goes in the mail: the *frontend's* `/verify` route, which posts the token
    back to `POST /auth/verify`.

    `public_base_url` is the public site origin (it is what the sitemap is built from), which
    is what a mail client has to be able to open — the API origin would be a JSON error page
    in the reader's browser."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/verify?{urlencode({'token': token})}"


async def issue(db: AsyncSession, *, user: User, settings: Settings) -> str:
    """Mint a verification token for `user` and commit it.

    Issuing does **not** retire the tokens already outstanding, which is the opposite of the
    obvious choice and is the security-relevant one: `POST /auth/verify/request` needs no
    session and takes a bare address, so anyone who knows Alice's address could otherwise
    invalidate the link sitting in her inbox by requesting another one — repeatably, and with
    the mail telling her to ask for a new one each time. Several live links cost nothing that
    matters: each is single-use, each expires on its own schedule, and every one of them is
    proof of control of the same address."""
    token = new_email_token()
    await email_token_repo.create(
        db,
        token=token,
        user_id=user.id,
        purpose=VERIFY,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.verify_token_ttl_hours),
    )
    await db.commit()
    return token


async def send(mailer: Mailer, *, user: User, token: str, settings: Settings) -> MessageId | None:
    """Mail `user` their verification link. Returns the provider's id, or None if the send
    failed — see the module docstring for why that is not an exception."""
    try:
        return await mailer.send(
            to=user.email,
            template=VERIFY,
            context={
                "display_name": user.display_name,
                "product_name": settings.product_name,
                "verify_url": verify_url(token, settings),
                "expires_in_hours": settings.verify_token_ttl_hours,
            },
        )
    except (MailError, MailConfigurationError, MissingCredentialError, httpx.HTTPError):
        # The two `RuntimeError`s are in the list for the same reason as the rest: they are
        # raised when the gateway *builds* its transport, which happens on the first send —
        # i.e. after signup has committed. Boot validation makes them unlikely (D-30), not
        # impossible, and "unlikely" is not a reason to answer a successful signup with a 500.
        logger.exception("verification mail could not be sent to user_id=%s", user.id)
        return None


async def issue_and_send(
    db: AsyncSession, *, user: User, mailer: Mailer, settings: Settings
) -> MessageId | None:
    """Issue a token and mail it. What both signup and an explicit re-send request do."""
    token = await issue(db, user=user, settings=settings)
    return await send(mailer, user=user, token=token, settings=settings)


async def request(db: AsyncSession, *, email: str, mailer: Mailer, settings: Settings) -> None:
    """Re-send verification to `email`, if there is anything to send.

    Silent about both the unknown address and the already-verified one, because the route
    above answers the same way regardless: an endpoint that takes a bare email address and
    varies its response is an account-enumeration oracle, and this one is reachable without a
    session by design (the mail can be opened on a device that was never signed in)."""
    user = await user_repo.get_by_email(db, email)
    if user is None or is_verified(user):
        return
    await issue_and_send(db, user=user, mailer=mailer, settings=settings)


async def consume(db: AsyncSession, *, token: str) -> User:
    """Spend `token` and mark its user verified. Raises `InvalidToken` if it is unknown,
    already spent, expired, or was issued for a different purpose.

    Verifying an already-verified user is not an error and not a second write: the token was
    live, so the holder did prove control of the address, and `email_verified_at` keeps the
    date of the first proof rather than the latest click."""
    now = datetime.now(UTC)
    row = await email_token_repo.get_live(db, token=token, purpose=VERIFY, now=now)
    if row is None:
        raise InvalidToken()
    user = await user_repo.get_by_id(db, row.user_id)
    if user is None:  # pragma: no cover  -- defensive: FK cascade prevents this
        raise InvalidToken()
    await email_token_repo.consume(db, row=row, consumed_at=now)
    if not is_verified(user):
        await user_repo.mark_email_verified(db, user, verified_at=now)
    await db.commit()
    return user
