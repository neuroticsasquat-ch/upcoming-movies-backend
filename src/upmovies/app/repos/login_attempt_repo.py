"""Tracks failed login attempts so account_service can lock out brute-force
guessers. Repo: pure DB I/O, no business logic, no commits."""

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import LoginAttempt


async def record(db: AsyncSession, *, email: str, ip: str | None) -> None:
    """Insert a failed-login row. Caller commits."""
    db.add(LoginAttempt(email=email, ip=ip))
    await db.flush()


async def count_since(db: AsyncSession, *, email: str, since: datetime) -> int:
    """Number of failed attempts for `email` recorded at or after `since`."""
    result = await db.execute(
        select(func.count())
        .select_from(LoginAttempt)
        .where(LoginAttempt.email == email, LoginAttempt.attempted_at >= since)
    )
    return result.scalar_one()


async def clear_for_email(db: AsyncSession, *, email: str) -> None:
    """Delete all failure rows for an email — call after a successful login."""
    await db.execute(delete(LoginAttempt).where(LoginAttempt.email == email))
