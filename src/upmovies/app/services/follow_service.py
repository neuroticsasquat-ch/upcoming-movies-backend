"""Following and unfollowing (D-10).

A follow produces timeline rows and nothing else; the derived-watchlist pass that a new follow
triggers (D-13) is NEU-1352's, and hooks in here when it lands. The rules live in a service
rather than the router because the imports (D-15, D-16) create follows too, with their own
`source`, and should be calling `follow` rather than restating the existence check."""

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.models import Follow, User
from upmovies.app.repos import follow_repo


async def follow(
    db: AsyncSession, *, user: User, entity_type: str, entity_id: str, source: str = "manual"
) -> tuple[Follow, bool]:
    """Follow `entity_id`, commit, and say whether the row is new.

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
