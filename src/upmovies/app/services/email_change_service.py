"""Moving an account's address, with both inboxes involved.

The third and last of M1's mailed-token flows, and the only one where the token is issued to
an address the account does not yet own. It borrows the shape the first two set
(`verification_service`'s module docstring is the long form): the token row is committed
before the mail is handed to the provider, and a provider failure is logged rather than
raised.

What is new here is that **a change is never single-sided**. Three separate gates stand
between a request and a moved address, and each closes a case the others do not:

* **The current password**, at request time. The route is authed and CSRF-guarded already, so
  this is not about cross-site posts; it is about a session left open on a borrowed machine.
  An address change is the one account edit that takes the account with it.
* **Confirmation from the new address**, which is what actually performs the move. Until the
  link in that inbox is opened, nothing about the account has changed. A user who types a
  colleague's address by mistake has mailed that colleague a link, not handed them a login.
* **A courtesy notice to the old address**, sent at request time rather than after the fact.
  This is the half that matters when the password has already leaked: the owner is told where
  their account is being taken *while the move can still be refused*, by resetting or changing
  the password, either of which calls `retire_pending` below. A notice sent after the swap
  would be an obituary.

The notice is best-effort like every other send here, and that deserves its own line because
the inherited rule was argued for a *convenience* mail. If the provider drops this one, the
flow quietly degrades to the single-sided change the ticket says must not happen. Two things
make that the right trade anyway: the confirmation link is the thing that moves the account
and it is equally likely to have been dropped, and raising would answer a request the token
row has already committed to with a 500, leaving a live link and a caller told it failed. The
mitigation is ordering — `request` mails the notice first — and a log line loud enough to find.

Sessions are deliberately left alone, unlike a password change, which drops all of them. The
confirm route is reached from the new inbox on a device that may never have signed in, and
proving control of an address says nothing about the browser holding the session being
someone else's. The user who started the change is the user finishing it."""

import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import EmailInUse, InvalidCredentials, InvalidToken
from upmovies.app.models import User
from upmovies.app.passwords import verify_password
from upmovies.app.repos import email_token_repo, login_attempt_repo, user_repo
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

_WHITESPACE = re.compile(r"\s+")

# The `purpose` these tokens carry in `app.email_token`, and the name of the template that
# mails them to the new address — one string for both, as in the sibling services, so a typo
# is an import error rather than a token nobody can spend. It is also what stops a
# verification or reset link from being spendable here: `get_live` matches on it.
EMAIL_CHANGE = "email_change"

# The template for the courtesy mail to the *old* address. A second template rather than a
# second purpose: it carries no token and nothing consumes it, so there is no row behind it.
EMAIL_CHANGE_NOTICE = "email_change_notice"


def email_change_url(token: str, settings: Settings) -> str:
    """The link that goes in the mail to the new address: the *frontend's* `/email-change`
    route, which posts the token back to `POST /auth/email-change/confirm`.

    `public_base_url` for the reason the sibling flows use it — the API origin would open as a
    JSON error page in the reader's mail client."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/email-change?{urlencode({'token': token})}"


async def _address_is_taken(db: AsyncSession, *, email: str) -> bool:
    """Whether any account already holds `email`.

    Asked of the database rather than compared in Python because `app.user.email` is CITEXT:
    `Taken@example.com` and `taken@example.com` are one address to the unique constraint, and a
    check that did not agree with the constraint would mint tokens that can only fail at the
    end of the flow.

    Deliberately not scoped to *other* accounts. A user asking to move to the address they
    already have is asking for nothing, and the honest answer is the same one the constraint
    would give: that address is taken. Excusing it would mean mailing a confirmation link for a
    change with no effect."""
    return await user_repo.get_by_email(db, email) is not None


def _one_line(value: str) -> str:
    """`value` with every run of whitespace collapsed to a single space.

    Applied to `display_name` before it reaches either template here. `SignupRequest` now
    refuses control characters outright, which is the real fix, but it cannot reach rows
    written before it existed — and this is the one flow that mails an address the caller
    merely names, so a stored newline here is arbitrary text delivered over the product's own
    sending domain to a stranger. The plain-text part is where it would land, because
    autoescape is off there by design (`mail/templates.py`).

    Collapsing the *value* rather than the rendered body, which the subject line already does
    for the same reason: the body's own newlines are the template's layout."""
    return _WHITESPACE.sub(" ", value).strip()


async def issue(db: AsyncSession, *, user: User, new_email: str, settings: Settings) -> str:
    """Mint an email-change token carrying `new_email` and commit it.

    Issuing does not retire the outstanding ones, matching both siblings. The argument that
    drives that choice there — an unauthenticated route must not let a stranger invalidate the
    link in someone's inbox — does not apply to a route behind a session and a password, but
    the behaviour is kept anyway: someone who requests a change, mistypes, and requests again
    should not find that the second mail killed the first while they were still reading it.
    The exposure is closed at the other end, in `confirm`, exactly as it is for a reset."""
    token = new_email_token()
    await email_token_repo.create(
        db,
        token=token,
        user_id=user.id,
        purpose=EMAIL_CHANGE,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.email_change_token_ttl_hours),
        new_email=new_email,
    )
    await db.commit()
    return token


async def send_confirmation(
    mailer: Mailer, *, user: User, new_email: str, token: str, settings: Settings
) -> MessageId | None:
    """Mail the confirmation link to the *new* address. Returns the provider's id, or None if
    the send failed — see the module docstring for why that is not an exception."""
    try:
        return await mailer.send(
            to=new_email,
            template=EMAIL_CHANGE,
            context={
                "display_name": _one_line(user.display_name),
                "product_name": settings.product_name,
                "current_email": user.email,
                "new_email": new_email,
                "email_change_url": email_change_url(token, settings),
                "expires_in_hours": settings.email_change_token_ttl_hours,
            },
        )
    except (MailError, MailConfigurationError, MissingCredentialError, httpx.HTTPError):
        logger.exception("email-change confirmation could not be sent for user_id=%s", user.id)
        return None


async def send_notice(
    mailer: Mailer, *, user: User, new_email: str, settings: Settings
) -> MessageId | None:
    """Tell the *old* address that a change was requested, and where to.

    Carries no link and no token: its job is to warn, and a second actionable mail would give
    an attacker who holds the old inbox a way to complete the change the account owner is being
    warned about."""
    try:
        return await mailer.send(
            to=user.email,
            template=EMAIL_CHANGE_NOTICE,
            context={
                "display_name": _one_line(user.display_name),
                "product_name": settings.product_name,
                "current_email": user.email,
                "new_email": new_email,
                "expires_in_hours": settings.email_change_token_ttl_hours,
            },
        )
    except (MailError, MailConfigurationError, MissingCredentialError, httpx.HTTPError):
        logger.exception("email-change notice could not be sent to user_id=%s", user.id)
        return None


async def request(
    db: AsyncSession,
    *,
    user: User,
    new_email: str,
    current_password: str,
    mailer: Mailer,
    settings: Settings,
) -> None:
    """Start a change: check the password, mint the token, mail both inboxes.

    Raises `InvalidCredentials` if the password is wrong and `EmailInUse` if the address
    belongs to another account or is already this one's.

    Answering `EmailInUse` at all is a deliberate departure from `/auth/reset/request` and
    `/auth/verify/request`, which are silent about which addresses exist. Those are
    unauthenticated and take a bare address, so any variation in their answer is an
    enumeration oracle for anyone at all.

    What makes this one acceptable is **not** that it costs the attacker an account and a
    password — it costs them *their own* account and *their own* password, which is to say
    nothing per probe. It is that `POST /auth/signup` already answers 409 `email_in_use`, so
    the same oracle is reachable without this route at all, and staying silent here would buy
    no secrecy while stranding a legitimate user in the one case they cannot diagnose: a
    typo'd address that happens to belong to somebody else, after which no mail ever arrives
    and nothing explains why.

    Throttling is the M1 rate limiter's job (D-19, NEU-1344); this route does none of its own
    yet, the same caveat its two siblings carry."""
    if not verify_password(current_password, user.password_hash):
        raise InvalidCredentials()
    if await _address_is_taken(db, email=new_email):
        raise EmailInUse()

    token = await issue(db, user=user, new_email=new_email, settings=settings)
    # The notice goes first, and the order is load-bearing rather than incidental. Both sends
    # swallow provider faults, but only the caught ones: an exception outside that tuple
    # raised while mailing the *confirmation* would, in the other order, skip the warning and
    # leave the account's owner unwarned about a change someone else started. Warning first
    # fails safe — the worst case becomes a warned owner and no link, which is the direction
    # this flow should fail in.
    await send_notice(mailer, user=user, new_email=new_email, settings=settings)
    await send_confirmation(mailer, user=user, new_email=new_email, token=token, settings=settings)


async def retire_pending(db: AsyncSession, *, user_id: UUID, now: datetime) -> None:
    """Make every outstanding change link for this account unspendable. Caller commits.

    Called from the two flows that reset the credential a change was authorised with —
    `account_service.change_password` and `reset_service.consume`. Without this the courtesy
    notice would be a lie: it tells the owner of the old address that changing the password
    stops the move, and a token in someone else’s inbox does not care about the password it
    was minted under. Changing it has to actually revoke the pending move, or the only advice
    the warning can give is advice that does not work."""
    await email_token_repo.retire_live_for_user(db, user_id=user_id, purpose=EMAIL_CHANGE, now=now)


async def confirm(db: AsyncSession, *, token: str) -> User:
    """Spend `token` and move the account to the address it carries.

    Raises `InvalidToken` if the token is unknown, already spent, expired, or issued for
    another purpose — one error for four causes, because telling the holder which one applies
    says whether the token ever existed. Raises `EmailInUse` if the address was claimed by
    somebody else between the request and the click; that case is distinguishable and worth
    distinguishing, because it is the one the user can act on by picking another address.

    The address becomes verified in the same write. Opening this link *is* proof of control of
    the address, which is the same proof `/auth/verify` accepts, so an account that has just
    changed its address is not asked to prove the identical thing twice.

    **Sibling change links are retired**, as a spent reset retires its siblings and for the
    same reason: after a change, a second live link pointing somewhere else is a standing key
    that would move the account again, and the person who just changed their address can
    neither see it nor revoke it."""
    now = datetime.now(UTC)
    # Locked, like a reset and unlike a verification: two links racing carry two different
    # addresses, and an unlocked read lets both through with the later commit silently winning.
    # Whoever loses gets `InvalidToken`, which is the answer they would have had a moment later.
    row = await email_token_repo.get_live(
        db, token=token, purpose=EMAIL_CHANGE, now=now, for_update=True
    )
    if row is None:
        raise InvalidToken()
    if row.new_email is None:  # pragma: no cover  -- only this module writes this purpose
        raise InvalidToken()
    user = await user_repo.get_by_id(db, row.user_id)
    if user is None:  # pragma: no cover  -- defensive: FK cascade prevents this
        raise InvalidToken()
    if await _address_is_taken(db, email=row.new_email):
        # Left unspent on purpose: the token is still the only thing standing between this
        # account and an address that may well be free again later, and burning it would make
        # a race someone else won cost the user the whole flow.
        raise EmailInUse()

    await email_token_repo.consume(db, row=row, consumed_at=now)
    await retire_pending(db, user_id=user.id, now=now)
    # Clear the brute-force lockout on *both* addresses, for the reason `reset_service.consume`
    # clears it on one. `authenticate` counts failures by email and records them even for
    # addresses with no account behind them, so failed logins accumulated against the new
    # address — most plausibly the owner's own typos at the sign-in form while they were
    # mid-change — would transfer onto the account the instant it moves, and refuse them their
    # own correct password with `InvalidCredentials`. The old address is cleared too so that
    # its rows are not left behind to be inherited by whoever signs up on it next. Spending a
    # mailed token is the stronger proof, the same argument a reset makes.
    await login_attempt_repo.clear_for_email(db, email=row.new_email)
    await login_attempt_repo.clear_for_email(db, email=user.email)
    await user_repo.update_email(db, user, email=row.new_email, verified_at=now)
    try:
        await db.commit()
    except IntegrityError as err:
        # The unique constraint is the last word, and it has to be: the check above reads
        # committed rows, so an address claimed by a signup that commits between that read and
        # this one arrives here. Same answer as the check, from the authority that decides it.
        await db.rollback()
        raise EmailInUse() from err
    return user
