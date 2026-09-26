from datetime import datetime
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import User


async def create(
    db: AsyncSession,
    *,
    email: str,
    password_hash: str,
    display_name: str,
) -> User:
    """Add a new user row and flush so that generated fields (id, created_at)
    are populated. Caller is responsible for committing."""
    user = User(email=email, password_hash=password_hash, display_name=display_name)
    db.add(user)
    await db.flush()
    return user


async def get_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    return await db.get(User, user_id)


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def delete_user(db: AsyncSession, user_id: UUID) -> None:
    await db.execute(sa_delete(User).where(User.id == user_id))


async def update_password_hash(db: AsyncSession, user: User, new_hash: str) -> None:
    """Set the password_hash attribute on the loaded model. Caller commits."""
    user.password_hash = new_hash


async def mark_email_verified(db: AsyncSession, user: User, *, verified_at: datetime) -> None:
    """Stamp the address as confirmed on the loaded model. Caller commits."""
    user.email_verified_at = verified_at


async def update_email(db: AsyncSession, user: User, *, email: str, verified_at: datetime) -> None:
    """Move the account to `email` and stamp it confirmed, on the loaded model. Caller commits.

    The two writes are one call because there is no case for either alone: the only way an
    address changes is by the new one proving control of itself, so a changed address is a
    verified address by construction. Splitting them would make "set the email without
    verifying it" expressible, and the M1 contract has no such state."""
    user.email = email
    user.email_verified_at = verified_at


async def list_page(
    db: AsyncSession, *, limit: int, offset: int, email_query: str | None = None
) -> tuple[list[User], int]:
    """One page of accounts, newest first, with the total the page was drawn from.

    The total comes back alongside the rows because the admin grant page (NEU-1392) needs to
    know whether there is a next page, and a search over an unknown number of accounts cannot
    be paged from the row count alone.

    `email_query` is a case-insensitive substring match. `email` is CITEXT, so `contains`
    already compares case-insensitively without a `lower()` on either side. `autoescape` is on
    because the value comes from a search box: a `%` or `_` typed into it is a character
    someone is looking for in an address, not a wildcard they meant to write."""
    filters = []
    if email_query:
        filters.append(User.email.contains(email_query, autoescape=True))

    total = await db.scalar(select(func.count()).select_from(User).where(*filters))
    rows = await db.execute(
        select(User)
        .where(*filters)
        .order_by(User.created_at.desc(), User.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars()), total or 0


async def set_entitled_until(
    db: AsyncSession, user: User, *, entitled_until: datetime | None
) -> None:
    """Grant, extend, or end this account's access, on the loaded model. Caller commits.

    One setter for all three because they are one write: a grant and an extension differ only
    in what was there before, and a revoke is `None` (D-38 — a past timestamp ends a grant just
    as well, and nothing here deletes a row). Deliberately touches nothing else: follows,
    settings and the iCal token survive a revoke untouched, so a later grant restores the
    account exactly as it was (D-40)."""
    user.entitled_until = entitled_until
