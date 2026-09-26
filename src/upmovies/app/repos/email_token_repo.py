"""Single-use mailed tokens. Repo: pure DB I/O, no commits, no business rules."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import EmailToken


async def create(
    db: AsyncSession,
    *,
    token: str,
    user_id: UUID,
    purpose: str,
    expires_at: datetime,
    new_email: str | None = None,
) -> EmailToken:
    """`new_email` is the address an email-change token moves the account to, and defaults to
    None because the other two purposes have nothing to carry (see the `EmailToken`
    docstring)."""
    row = EmailToken(
        token=token,
        user_id=user_id,
        purpose=purpose,
        expires_at=expires_at,
        new_email=new_email,
    )
    db.add(row)
    await db.flush()
    return row


async def get_live(
    db: AsyncSession, *, token: str, purpose: str, now: datetime, for_update: bool = False
) -> EmailToken | None:
    """The token row only if it is spendable: right purpose, unconsumed, unexpired.

    The three conditions are one query rather than a fetch and three checks at the call site
    so that "this token cannot be spent" has exactly one answer, and so the caller cannot
    accidentally report *which* of the three failed — reused, expired and never-existed are
    the same 4xx to the holder of the link on purpose.

    `for_update` takes a row lock, which is what makes single-use hold when the same token
    arrives twice at once. Off by default because it costs a lock and verification does not
    need it: spending a verify token twice converges on the same state. A reset does not — the
    two callers set *different* passwords — so `reset_service` asks for the lock. Under
    Postgres' READ COMMITTED the second caller blocks here, then re-checks this WHERE against
    the row the first one committed, sees `consumed_at` set, and gets `None`."""
    stmt = select(EmailToken).where(
        EmailToken.token == token,
        EmailToken.purpose == purpose,
        EmailToken.consumed_at.is_(None),
        EmailToken.expires_at > now,
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def retire_live_for_user(
    db: AsyncSession, *, user_id: UUID, purpose: str, now: datetime
) -> None:
    """Mark every still-spendable token of `purpose` for this user as consumed.

    Scoped to one purpose because the flows are independent: finishing a reset says nothing
    about a pending address-verification link, and retiring it would make the two tokens in
    someone's inbox invalidate each other in arrival order.

    A bulk UPDATE rather than a load-and-mutate loop, and `synchronize_session=False` with it:
    the caller has at most its own row loaded — which it has already consumed and flushed, so
    the `consumed_at IS NULL` filter passes over it — and there is nothing else in the
    identity map for the session to keep in step."""
    await db.execute(
        sa_update(EmailToken)
        .where(
            EmailToken.user_id == user_id,
            EmailToken.purpose == purpose,
            EmailToken.consumed_at.is_(None),
            EmailToken.expires_at > now,
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.flush()


async def consume(db: AsyncSession, *, row: EmailToken, consumed_at: datetime) -> None:
    """Mark the token spent. The caller holds the row from `get_live` inside the same
    transaction as whatever consuming it does, so we mutate in place."""
    row.consumed_at = consumed_at
    await db.flush()
