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

The read-only half that pass needs — this user's settings, or the D-33 defaults unpersisted
when there is no row — is deliberately not built here, because nothing calls it yet and an
unused accessor is a guess at its signature. NEU-1379 adds it, *in this module* rather than by
re-deriving `weekly` at the call site: the defaults are D-33's and must be spelled once."""

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


async def set_digest_cadence(db: AsyncSession, *, user: User, digest_cadence: str) -> UserSettings:
    """Set how often this user is digested, and commit. Creates the row if this is the first
    thing they ever did with it — a PATCH from a client that never issued the GET is a perfectly
    ordinary first touch, and refusing it would make the settings screen's order load-bearing."""
    row = await get_or_create(db, user=user)
    await user_settings_repo.set_digest_cadence(db, row, digest_cadence=digest_cadence)
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
