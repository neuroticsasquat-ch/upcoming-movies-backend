"""Invite codes: issuing them, and redeeming one during signup.

Since NEU-1343 an invite is no longer what gets someone in — Turnstile is (D-18) — it is the
admin comp path, handed to someone specific. That is also why redemption moved here from
`account_service.signup`, where it used to live because it is part of the user-creation
transaction: with the invite optional, the signup path now reads as two independent questions
("may this caller sign up at all", "is there a code to spend"), and the second one's rules
belong with the rest of the invite rules rather than inlined in the middle of the first.

Nothing about the transaction changed with the move. `redeem` and `consume` flush and do not
commit, in the repo layer's manner — the caller still owns the transaction, so an invite is
still spent in the same commit that creates the user it was spent on, or not at all.
"""

import secrets
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import InvalidInvite
from upmovies.app.models import Invite
from upmovies.app.repos import invite_repo


def _new_code() -> str:
    """A URL-safe random code with enough entropy that brute-forcing is impractical."""
    return secrets.token_urlsafe(16)


async def create_invite(db: AsyncSession, *, email_hint: str | None = None) -> Invite:
    """Generate a fresh invite, persist it, commit. Returns the row."""
    code = _new_code()
    invite = await invite_repo.create(db, code=code, email_hint=email_hint)
    await db.commit()
    return invite


async def list_invites(db: AsyncSession) -> list[Invite]:
    """Admin-only listing of every invite ever issued."""
    return await invite_repo.list_all(db)


async def redeem(db: AsyncSession, *, code: str, email: str) -> Invite:
    """The invite `code` grants `email` a signup, or `InvalidInvite`.

    Unknown, already consumed and issued-to-another-address are one error, as they have been
    since this check was written: distinguishing them would let the signup route answer which
    codes were ever issued, and the person holding a code they cannot spend needs the same
    thing in all three cases — to ask whoever gave it to them.

    Does not mark the invite spent; `consume` does that, after the user row exists."""
    invite = await invite_repo.get(db, code)
    if invite is None or invite.consumed_at is not None:
        raise InvalidInvite()
    if invite.email_hint is not None and invite.email_hint.lower() != email.lower():
        raise InvalidInvite()
    return invite


async def consume(db: AsyncSession, *, invite: Invite, user_id: UUID, now: datetime) -> None:
    """Spend a redeemed invite on the user it just created. No commit: the caller's
    transaction is what makes this and the user row land together."""
    await invite_repo.consume(db, invite=invite, user_id=user_id, consumed_at=now)
