"""Web Push registrations (D-36). Repo: pure DB I/O, no commits, no business rules."""

from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import PushSubscription


async def upsert(
    db: AsyncSession,
    *,
    user_id: UUID,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> PushSubscription:
    """Register this endpoint to this user, replacing whatever was registered to it before.

    `ON CONFLICT (endpoint) DO UPDATE` rather than `DO NOTHING`, and the update includes
    `user_id`, which is the case worth stating: a browser profile is one endpoint, so when a
    second account subscribes from a machine the first one used, the row has to *move*. Leaving
    it would push the first user's watchlist alerts to whoever is signed in now — and a
    notification, unlike a mail, is read on a lock screen by whoever is holding the phone.

    The keys are refreshed on the same statement because a browser re-subscribing after a
    service-worker update sends the same endpoint with fresh key material; keeping the old
    `p256dh` would encrypt every later payload to a key the browser can no longer read."""
    row = await db.scalar(
        pg_insert(PushSubscription)
        .values(
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=user_agent,
        )
        .on_conflict_do_update(
            index_elements=["endpoint"],
            set_={
                "user_id": user_id,
                "p256dh": p256dh,
                "auth": auth,
                "user_agent": user_agent,
            },
        )
        .returning(PushSubscription)
    )
    assert row is not None  # an upsert always leaves a row to return
    return row


async def delete_for_user(db: AsyncSession, *, user_id: UUID, endpoint: str) -> int:
    """Unsubscribe one endpoint of this user's. Returns how many rows went.

    Scoped to the owner: an endpoint is a long unguessable URL, but it is also a string a
    client sends us, and deleting by endpoint alone would let one account unsubscribe
    another's device."""
    result = await db.execute(
        delete(PushSubscription).where(
            PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint
        )
    )
    return result.rowcount or 0  # type: ignore[attr-defined]  # CursorResult has rowcount


async def delete_by_endpoint(db: AsyncSession, *, endpoint: str) -> int:
    """Drop a subscription the push service has told us is gone (404/410).

    Unscoped, deliberately, unlike `delete_for_user`: the authority here is the push service
    saying the endpoint no longer exists, which is true regardless of who it currently belongs
    to."""
    result = await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))
    return result.rowcount or 0  # type: ignore[attr-defined]  # CursorResult has rowcount


async def any_exist(db: AsyncSession) -> bool:
    """Whether this deployment has any push registration at all.

    The boot check's question (`push.validate_push_configuration`), and an `EXISTS` rather than
    a count because the answer is a yes/no and the table has no other reason to be scanned at
    startup."""
    return bool(await db.scalar(select(exists().where(PushSubscription.id.is_not(None)))))
