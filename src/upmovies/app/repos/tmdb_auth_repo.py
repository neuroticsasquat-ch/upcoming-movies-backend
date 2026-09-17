"""`app.tmdb_auth_request` rows (D-16). Repo: pure DB I/O, no commits, no business rules."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import TmdbAuthRequest

TOKEN_TTL = timedelta(minutes=15)
"""How long a request token this end will still answer for (spec §2).

TMDB expires its own tokens in sixty minutes, and this is deliberately shorter: the window is
how long a user takes to read one approve screen, and a row that outlives that is a token
nobody is coming back for. Shortening it is free — the remedy is the same one-click start
route — while lengthening it only widens the replay window the row exists to close."""


async def create(db: AsyncSession, *, request_token: str, user_id: UUID) -> TmdbAuthRequest:
    """Record that this user was sent off to approve this token. Caller commits."""
    row = TmdbAuthRequest(request_token=request_token, user_id=user_id)
    db.add(row)
    await db.flush()
    return row


async def get(db: AsyncSession, request_token: str) -> TmdbAuthRequest | None:
    return await db.get(TmdbAuthRequest, request_token)


async def delete_token(db: AsyncSession, request_token: str) -> None:
    """Spend the token. Caller commits."""
    await db.execute(delete(TmdbAuthRequest).where(TmdbAuthRequest.request_token == request_token))


async def prune_expired(db: AsyncSession, *, now: datetime | None = None) -> None:
    """Drop every token past its TTL, whoever it belongs to. Caller commits.

    Swept on the next `start` rather than on a schedule: the table only grows when someone
    begins an approve flow, so the one route that grows it is the natural place to pay for it,
    and a user who never comes back leaves a row that the next user's start clears."""
    cutoff = (now or datetime.now(UTC)) - TOKEN_TTL
    await db.execute(delete(TmdbAuthRequest).where(TmdbAuthRequest.created_at < cutoff))


def is_expired(row: TmdbAuthRequest, *, now: datetime | None = None) -> bool:
    """Whether this row is past the TTL — the rule `prune_expired` deletes on, applied to a row
    the prune has not reached yet.

    Both halves are needed: pruning alone would let a token approved an hour ago still work if
    no one had started a flow since, and the check alone would leave the table to grow."""
    return (now or datetime.now(UTC)) - row.created_at > TOKEN_TTL
