"""Single-use mailed tokens. Repo: pure DB I/O, no commits, no business rules."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import EmailToken


async def create(
    db: AsyncSession,
    *,
    token: str,
    user_id: UUID,
    purpose: str,
    expires_at: datetime,
) -> EmailToken:
    row = EmailToken(token=token, user_id=user_id, purpose=purpose, expires_at=expires_at)
    db.add(row)
    await db.flush()
    return row


async def get_live(
    db: AsyncSession, *, token: str, purpose: str, now: datetime
) -> EmailToken | None:
    """The token row only if it is spendable: right purpose, unconsumed, unexpired.

    The three conditions are one query rather than a fetch and three checks at the call site
    so that "this token cannot be spent" has exactly one answer, and so the caller cannot
    accidentally report *which* of the three failed — reused, expired and never-existed are
    the same 4xx to the holder of the link on purpose."""
    result = await db.execute(
        select(EmailToken).where(
            EmailToken.token == token,
            EmailToken.purpose == purpose,
            EmailToken.consumed_at.is_(None),
            EmailToken.expires_at > now,
        )
    )
    return result.scalar_one_or_none()


async def consume(db: AsyncSession, *, row: EmailToken, consumed_at: datetime) -> None:
    """Mark the token spent. The caller holds the row from `get_live` inside the same
    transaction as whatever consuming it does, so we mutate in place."""
    row.consumed_at = consumed_at
    await db.flush()
