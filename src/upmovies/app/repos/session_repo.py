from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import Session


async def create(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: UUID,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
) -> None:
    """Insert a new session row. Caller is responsible for committing."""
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    db.add(
        Session(
            id=session_id,
            user_id=user_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
    )


async def get_active(db: AsyncSession, session_id: str) -> Session | None:
    """Return the session row only if it exists and has not expired."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(Session).where(Session.id == session_id, Session.expires_at > now)
    )
    return result.scalar_one_or_none()


async def touch(db: AsyncSession, session_id: str) -> None:
    """Bump last_seen_at to now."""
    await db.execute(
        update(Session).where(Session.id == session_id).values(last_seen_at=datetime.now(UTC))
    )


async def delete(db: AsyncSession, session_id: str) -> None:
    await db.execute(sa_delete(Session).where(Session.id == session_id))


async def delete_all_for_user(db: AsyncSession, user_id: UUID) -> None:
    await db.execute(sa_delete(Session).where(Session.user_id == user_id))
