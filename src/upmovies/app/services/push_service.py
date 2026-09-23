"""Registering and unregistering a browser for Web Push (D-36).

Thin over the repo, and in a service anyway for the reason the layering rule gives: the routes
own a request, not a transaction, and both of these commit. What is *not* here is any notion of
"the user's push preference" — D-36 has none. A subscription's existence is the preference, and
turning notifications off is deleting the row, which is what makes the toggle in the settings
screen (NEU-1388) a single call rather than a flag plus a registration.

Revocation is the other side of that and belongs to nobody here: a lapsed grant leaves these
rows alone (D-40) and is answered upstream, where the decision pass declines to queue for an
unentitled user. The only other thing that deletes a row is the push service saying the
endpoint is gone — see `push_sender`."""

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import PushSubscription, User
from upmovies.app.repos import push_subscription_repo


async def subscribe(
    db: AsyncSession,
    *,
    user: User,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> PushSubscription:
    """Register (or re-register) one browser, and commit.

    Idempotent by endpoint: a browser that subscribes twice — a reload, a service-worker
    update, a client that posts on every page load — holds one row afterwards, with its
    latest keys."""
    row = await push_subscription_repo.upsert(
        db, user_id=user.id, endpoint=endpoint, p256dh=p256dh, auth=auth, user_agent=user_agent
    )
    await db.commit()
    return row


async def unsubscribe(db: AsyncSession, *, user: User, endpoint: str) -> bool:
    """Unregister one of this user's browsers, and commit. False if it was not registered.

    The caller renders both outcomes as a 204: unsubscribing a browser that is already
    unsubscribed is the state the client asked for, and a client that has just torn down its
    own registration legitimately does not know whether we still held it."""
    deleted = await push_subscription_repo.delete_for_user(db, user_id=user.id, endpoint=endpoint)
    await db.commit()
    return deleted > 0
