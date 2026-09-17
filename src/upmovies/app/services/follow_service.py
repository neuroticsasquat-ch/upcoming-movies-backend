"""Following and unfollowing (D-10).

A follow produces timeline rows and, through `derivation_service`, the watchlist items D-13
derives from it. The rules live in a service rather than the router because the imports (D-15,
D-16) create follows too, with their own `source`, and should be calling `follow` rather than
restating the existence check — and because the derivation below has to happen for every caller
that creates a follow, not just the route."""

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.models import Follow, User
from upmovies.app.repos import follow_repo
from upmovies.app.services import derivation_service


async def follow(
    db: AsyncSession, *, user: User, entity_type: str, entity_id: str, source: str = "manual"
) -> tuple[Follow, bool]:
    """Follow `entity_id`, commit, and say whether the row is new.

    A new follow derives its watchlist items in the same transaction (D-13), so a user who
    follows a director sees their in-play films on the watchlist by the time the request
    answers. It shares the caller's transaction on purpose: a derivation that fails takes the
    follow with it, and the user retries one action rather than being left with a follow whose
    items silently wait for the next sweep.

    **Callers that write a watchlist item of their own must write it before the follow.** A
    title follow derives the film it names, so following first leaves a `derived_from_follow`
    item, and `watchlist_service.add` then returns that row untouched — the caller's `source`
    never lands, and the user's later removal of a film they explicitly added writes a
    dismissal (D-13). This binds D-15 and D-16: `NEU-1356-letterboxd-import.md` §3 lists the
    follow before the item for a matched `watchlist.csv` row, and that order has to flip.

    Only a *new* follow does: a second follow of the same entity has already been
    derived from, and re-deriving would at best write nothing and at worst put back a film the
    user has since dismissed — which the dismissal check would refuse anyway, making the work
    pure cost.

    Idempotent: a second follow of the same entity returns the existing row untouched — its
    `source` and `created_at` record the *first* time the user showed interest, and an import
    re-run must not rewrite a manual follow as an imported one (D-15). `NotFound` if the catalog
    has no such entity: a follow of a thing that does not exist would match nothing forever."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is not None:
        return existing, False
    if not await follow_repo.entity_exists(db, entity_type=entity_type, entity_id=entity_id):
        raise NotFound()
    created = await follow_repo.create(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id, source=source
    )
    await derivation_service.derive_for_follow(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    await db.commit()
    return created, True


async def list_follows(db: AsyncSession, *, user: User) -> list[Follow]:
    return await follow_repo.list_for_user(db, user.id)


async def unfollow(db: AsyncSession, *, user: User, entity_type: str, entity_id: str) -> None:
    """Delete the follow and commit. `NotFound` if there is none.

    Deletes the follow only. Watchlist items it derived stay (the user asked to be told about
    those films, and may have set prefs on them), and so does any dismissal."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is None:
        raise NotFound()
    await follow_repo.delete(db, existing)
    await db.commit()
