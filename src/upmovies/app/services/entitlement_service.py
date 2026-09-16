"""Granting and revoking access, and the audit line each one leaves (D-38).

The rules live here rather than in the router because granting is the one write that decides
whether an account can reach subscriber functionality at all, and that decision should be
expressible without an HTTP request around it — the billing project takes this over from a
payment-provider webhook, and *bl: Subscription & Billing* should be calling `grant` rather
than reimplementing it.

`app/entitlements.py` is the reading half of the same seam, and deliberately separate: it is
imported by every gated route and batch pass in M3 and M7, none of which have any business
being able to write the column."""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.models import User
from upmovies.app.repos import user_repo

logger = logging.getLogger(__name__)


async def list_accounts(
    db: AsyncSession, *, limit: int, offset: int, email_query: str | None = None
) -> tuple[list[User], int]:
    """One page of accounts for the grant page, with the total the page was drawn from."""
    return await user_repo.list_page(db, limit=limit, offset=offset, email_query=email_query)


async def grant(
    db: AsyncSession, *, user_id: UUID, entitled_until: datetime, granted_by: User
) -> User:
    """Set `user_id`'s access expiry, commit, and record who did it. `NotFound` if no such user.

    Grant and extend are one operation because they are one write — they differ only in what
    was there before, which is why the audit line carries the previous value. `granted_by` is
    required rather than optional: an entitlement change with no named actor is not worth
    logging, and there is no unattended writer of this column until billing lands."""
    user = await _load(db, user_id)
    previous = user.entitled_until
    await user_repo.set_entitled_until(db, user, entitled_until=entitled_until)
    await db.commit()
    logger.info(
        "entitlement granted by admin_id=%s to user_id=%s until=%s (was %s)",
        granted_by.id,
        user.id,
        entitled_until.isoformat(),
        previous.isoformat() if previous else None,
    )
    return user


async def revoke(db: AsyncSession, *, user_id: UUID, revoked_by: User) -> User:
    """Clear `user_id`'s access expiry, commit, and record who did it. `NotFound` if no such user.

    Clears one column and touches nothing else: follows, watchlist items, dismissals, settings
    and the iCal token survive, so a later grant restores the account rather than handing back
    an empty one (D-40). Setting a past `entitled_until` through `grant` ends access just as
    well and keeps the date — this is the variant for "there was never meant to be a grant here"."""
    user = await _load(db, user_id)
    previous = user.entitled_until
    await user_repo.set_entitled_until(db, user, entitled_until=None)
    await db.commit()
    logger.info(
        "entitlement revoked by admin_id=%s from user_id=%s (was %s)",
        revoked_by.id,
        user.id,
        previous.isoformat() if previous else None,
    )
    return user


async def _load(db: AsyncSession, user_id: UUID) -> User:
    user = await user_repo.get_by_id(db, user_id)
    if user is None:
        raise NotFound()
    return user
