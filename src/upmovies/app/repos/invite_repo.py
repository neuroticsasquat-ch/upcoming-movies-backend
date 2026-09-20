"""Invite codes that gate signup. Repo: pure DB I/O, no commits, no business rules."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import Invite, User


async def create(db: AsyncSession, *, code: str, email_hint: str | None) -> Invite:
    invite = Invite(code=code, email_hint=email_hint)
    db.add(invite)
    await db.flush()
    return invite


async def get(db: AsyncSession, code: str) -> Invite | None:
    return await db.get(Invite, code)


async def list_all(db: AsyncSession) -> list[tuple[Invite, str | None]]:
    """Every invite, newest first, each paired with the email of the account that spent it
    (`None` while outstanding, or once that account is gone: the FK is `ON DELETE SET NULL`).

    Resolved by an outer join rather than stored on the row — nothing about the invite changes
    when its consumer's address does, and the page wants the current address."""
    stmt = (
        select(Invite, User.email)
        .outerjoin(User, User.id == Invite.consumed_by_user_id)
        .order_by(Invite.created_at.desc())
    )
    rows = await db.execute(stmt)
    return [(invite, email) for invite, email in rows.all()]


async def consume(
    db: AsyncSession,
    *,
    invite: Invite,
    user_id: UUID,
    consumed_at: datetime,
) -> None:
    """Mark the invite as consumed. The caller already has the row loaded
    (typically inside the same transaction as user creation), so we mutate
    in place rather than re-querying."""
    invite.consumed_at = consumed_at
    invite.consumed_by_user_id = user_id
    await db.flush()
