"""Issuing, mailing and spending password-reset tokens.

The second of M1's three mailed-token flows, and it borrows verification's shape wholesale
(`verification_service`'s module docstring is the long form): the token row is committed
before the mail is handed to the provider, and a provider failure is logged rather than
raised. Here the second half is not merely convenient but required — `POST /auth/reset/request`
answers 202 whether or not the address exists, so it cannot report a send failure without also
reporting that there was something to send.

Where this flow deliberately parts company with verification is what a spent token *does*.
Verifying stamps a column. Resetting sets the password and drops every session the account
has, which means a live reset token is the account. Three things follow, and each is written
down where it happens below: the window is short (`reset_token_ttl_hours`), spending one
retires the others, and the route hands back nothing."""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import InvalidToken
from upmovies.app.models import User
from upmovies.app.passwords import hash_password
from upmovies.app.repos import email_token_repo, login_attempt_repo, session_repo, user_repo
from upmovies.app.services import email_change_service
from upmovies.app.tokens import new_email_token
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
# that sends them — one string for both, as in `verification_service`, so a typo is an import
# error rather than a token nobody can spend. It is also the value that stops a verification
# link from being spendable here: `get_live` matches on it.
RESET = "reset"


def reset_url(token: str, settings: Settings) -> str:
    """The link that goes in the mail: the *frontend's* `/reset` route, which collects the new
    password and posts it with the token to `POST /auth/reset`.

    `public_base_url` for the same reason verification uses it — the API origin would open as
    a JSON error page in the reader's mail client."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/reset?{urlencode({'token': token})}"


async def issue(db: AsyncSession, *, user: User, settings: Settings) -> str:
    """Mint a reset token for `user` and commit it.

    Issuing does **not** retire the tokens already outstanding, matching verification and for
    the same reason: this route needs no session and takes a bare address, so supersession on
    issue would let anyone who knows Alice's address invalidate the link sitting in her inbox,
    repeatedly. The exposure that argument glosses over for verification — several live
    credentials at once — is closed at the other end here instead, in `consume`, which is the
    half that can tell the legitimate holder from an attacker."""
    token = new_email_token()
    await email_token_repo.create(
        db,
        token=token,
        user_id=user.id,
        purpose=RESET,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.reset_token_ttl_hours),
    )
    await db.commit()
    return token


async def send(mailer: Mailer, *, user: User, token: str, settings: Settings) -> MessageId | None:
    """Mail `user` their reset link. Returns the provider's id, or None if the send failed —
    see the module docstring for why that is not an exception."""
    try:
        return await mailer.send(
            to=user.email,
            template=RESET,
            context={
                "display_name": user.display_name,
                "product_name": settings.product_name,
                "reset_url": reset_url(token, settings),
                "expires_in_hours": settings.reset_token_ttl_hours,
            },
        )
    except (MailError, MailConfigurationError, MissingCredentialError, httpx.HTTPError):
        logger.exception("password reset mail could not be sent to user_id=%s", user.id)
        return None


async def request(db: AsyncSession, *, email: str, mailer: Mailer, settings: Settings) -> None:
    """Mail a reset link to `email`, if there is an account behind it.

    Silent about the unknown address because the route above answers 202 either way. Note that
    an *unverified* address still gets a reset mail, unlike the digests D-31
    suppresses: the mail is not a notification the account opted into, it is the mechanism by
    which whoever holds the inbox proves they hold it, and refusing to send it would strand
    anyone who signed up, never clicked verify, and then forgot their password."""
    user = await user_repo.get_by_email(db, email)
    if user is None:
        return
    token = await issue(db, user=user, settings=settings)
    await send(mailer, user=user, token=token, settings=settings)


async def consume(db: AsyncSession, *, token: str, new_password: str) -> User:
    """Spend `token`, set `new_password`, and sign the account out everywhere.

    Raises `InvalidToken` if the token is unknown, already spent, expired, or was issued for
    another purpose — one error for four causes, because telling the holder which one applies
    says whether the token ever existed.

    The three writes are one transaction on purpose. A reset that set the password without
    dropping the sessions would leave whoever knew the old one still signed in, which is the
    case the route exists for; a reset that dropped the sessions without setting the password
    would lock out the legitimate user instead.

    **Sibling reset links are retired too.** This is the opposite of `issue`'s rule above, and
    the asymmetry is the point: retiring on *issue* punishes the victim for an unauthenticated
    request anyone can make, whereas retiring on *consume* happens only after someone has
    proved they hold the inbox. After a reset — the thing people do precisely because they
    fear the account is compromised — a second live reset link is a standing key that the
    person who just reset it can neither see nor revoke."""
    now = datetime.now(UTC)
    # Locked, unlike verification's read of the same helper: two resets racing on one token
    # set two different passwords, and an unlocked read lets both through with the later
    # commit silently winning. Whoever loses the race gets `InvalidToken`, which is the same
    # answer they would have got had they arrived a moment later.
    row = await email_token_repo.get_live(db, token=token, purpose=RESET, now=now, for_update=True)
    if row is None:
        raise InvalidToken()
    user = await user_repo.get_by_id(db, row.user_id)
    if user is None:  # pragma: no cover  -- defensive: FK cascade prevents this
        raise InvalidToken()

    await email_token_repo.consume(db, row=row, consumed_at=now)
    await email_token_repo.retire_live_for_user(db, user_id=user.id, purpose=RESET, now=now)
    # And any pending address change (NEU-1341), for a reason beyond the one that retires the
    # sibling reset links: a reset is the flow people reach for when they think the account is
    # compromised, and a live change link is precisely how an attacker who got in first keeps
    # it. The notice mailed to the old address promises that a new password stops the move.
    await email_change_service.retire_pending(db, user_id=user.id, now=now)
    await user_repo.update_password_hash(db, user, hash_password(new_password))
    await session_repo.delete_all_for_user(db, user.id)
    # And clear the brute-force lockout, which `authenticate` checks *before* it checks the
    # password. Without this the flow fails in its single most common case: failing login a
    # few times is the thing that sends someone to the reset form, so they arrive already at
    # the threshold, reset successfully, and are still refused their new password until the
    # window expires — with `InvalidCredentials`, which tells them the password they just set
    # is wrong. A successful login clears these rows for the same reason; spending a mailed
    # token is the stronger proof of the two.
    await login_attempt_repo.clear_for_email(db, email=user.email)
    await db.commit()
    return user
