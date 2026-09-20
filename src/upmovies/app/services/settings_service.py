"""A user's delivery settings: the lazily created row, the cadence write, and the token
rotation (D-33, D-34).

In a service rather than the routes because the row's *existence* is a rule, not a request
handler's detail — every entry point below creates it, so a PATCH from a client that never
issued the GET behaves the same as one that did.

**Everything here writes, and the batch passes must not use it as it stands.** The three
functions are the three things the `/me/settings` routes do, and all of them create-and-commit;
`get_or_create` is a write on a read path by design (D-33's lazy creation). That is safe for a
request made by the row's owner, and wrong for a pass that fans out over every user: the digest
pass (NEU-1379) would write and commit a settings row for each user it merely *considered*,
including the unentitled ones it is about to mark `suppressed` — turning the row from "this
subscriber opened their settings" into "this account was once looked at", and quietly leaving a
live `ical_token` on accounts that never held a grant (D-39, D-40).

The read-only half the batch passes need is not here and deliberately never has been: they
read the row as a **column** in the query that selects their users, `COALESCE`d over an outer
join to the D-33 and D-44 defaults — the digest pass for `digest_cadence`, the notify pass for
`alert_stores` (D-44). That is what keeps a pass that merely *considered* a user from leaving
them a settings row, and an accessor here would be the thing tempting it back."""

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app import tokens
from upmovies.app.models import User, UserSettings
from upmovies.app.repos import user_settings_repo


async def get_or_create(db: AsyncSession, *, user: User) -> UserSettings:
    """This user's settings, writing the defaults row the first time anyone asks (D-33).

    Creation is a write on a read path, so it commits: the token handed back is in a URL the
    caller may subscribe a calendar to before it makes another request, and a token that only
    existed inside an uncommitted transaction would resolve to nothing when they did."""
    existing = await user_settings_repo.get(db, user_id=user.id)
    if existing is not None:
        return existing
    row = await user_settings_repo.create_if_absent(
        db, user_id=user.id, ical_token=tokens.new_ical_token()
    )
    await db.commit()
    return row


async def update(
    db: AsyncSession,
    *,
    user: User,
    digest_cadence: str | None = None,
    alert_stores: list[str] | None = None,
) -> UserSettings:
    """Write the settings the caller named — either, or both — and commit.

    `None` means "not in this PATCH", not "clear it": an empty `alert_stores` list is a real
    answer (no availability alerts at all, D-44) and is written as one. The request model is
    what refuses a PATCH that names neither.

    Creates the row if this is the first thing the user ever did with it — a PATCH from a
    client that never issued the GET is a perfectly ordinary first touch, and refusing it would
    make the settings screen's order load-bearing."""
    row = await get_or_create(db, user=user)
    if digest_cadence is not None:
        await user_settings_repo.set_digest_cadence(db, row, digest_cadence=digest_cadence)
    if alert_stores is not None:
        await user_settings_repo.set_alert_stores(db, row, alert_stores=alert_stores)
    await db.commit()
    return row


async def rotate_ical_token(db: AsyncSession, *, user: User) -> UserSettings:
    """Issue this user a new calendar token, and commit (D-34).

    The old value stops resolving the moment this returns, which is the whole affordance: a
    calendar URL that has been shared or leaked cannot be un-shared, so the remedy is to make it
    name nothing. Every calendar already subscribed to the old URL breaks, deliberately — the
    user re-adds the new one."""
    row = await get_or_create(db, user=user)
    await user_settings_repo.set_ical_token(db, row, ical_token=tokens.new_ical_token())
    await db.commit()
    return row
