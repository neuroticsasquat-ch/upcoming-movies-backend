"""Invite codes that gate signup. Repo: pure DB I/O, no commits, no business rules."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import Invite


async def create(db: AsyncSession, *, code: str, email_hint: str | None) -> Invite:
    invite = Invite(code=code, email_hint=email_hint)
    db.add(invite)
    await db.flush()
    return invite


async def get(db: AsyncSession, code: str) -> Invite | None:
    return await db.get(Invite, code)


async def list_all(db: AsyncSession) -> list[Invite]:
    rows = await db.execute(select(Invite).order_by(Invite.created_at.desc()))
    return list(rows.scalars().all())


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
