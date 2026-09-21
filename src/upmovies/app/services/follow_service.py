"""Following and unfollowing (D-10). A follow is binary — there is nothing else to set (EF-1).

A follow is the only thing a user keeps (M8, ADR-0018): it feeds the timeline *and* the
alerts, and the watchlist is a query over it (`app.follow_queries`). Nothing is derived from
it any more — creating a follow writes one row, and every surface recomputes what that follow
reaches on read.

The rules live in a service rather than the router because the imports (D-15, D-16) create
follows too, with their own `source`, and should be calling `follow` rather than restating the
existence check."""

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.models import Follow, User
from upmovies.app.repos import follow_repo
from upmovies.app.repos.follow_repo import EntityLabel


async def follow(
    db: AsyncSession,
    *,
    user: User,
    entity_type: str,
    entity_id: str,
    source: str = "manual",
) -> tuple[Follow, EntityLabel | None, bool]:
    """Follow `entity_id`, commit, and say what it is called and whether the row is new.

    Idempotent: a second follow of the same entity returns the existing row **untouched**. Its
    `source` and `created_at` record the *first* time the user showed interest, and an import
    re-run must not rewrite a manual follow as an imported one (D-15).

    `NotFound` if the catalog has no such entity: a follow of a thing that does not exist would
    match nothing forever."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    label = await follow_repo.get_entity_label(db, entity_type=entity_type, entity_id=entity_id)
    if existing is not None:
        return existing, label, False
    if label is None:
        raise NotFound()
    created = await follow_repo.create(
        db,
        user_id=user.id,
        entity_type=entity_type,
        entity_id=entity_id,
        source=source,
    )
    await db.commit()
    return created, label, True


async def list_follows(db: AsyncSession, *, user: User) -> list[tuple[Follow, EntityLabel | None]]:
    """Every follow with the label its entity carries in the catalog, in one grouped lookup per
    entity type. A follow the catalog cannot resolve keeps its place in the list with a `None`
    label — see `FollowOut`."""
    follows = await follow_repo.list_for_user(db, user.id)
    labels = await follow_repo.entity_labels(db, [(f.entity_type, f.entity_id) for f in follows])
    return [(f, labels.get((f.entity_type, f.entity_id))) for f in follows]


async def get_follow(
    db: AsyncSession, *, user: User, entity_type: str, entity_id: str
) -> tuple[Follow, EntityLabel | None]:
    """One follow and its label, or `NotFound`. Reads only — nothing is committed.

    What is left of `set_coverage` now the tier is gone (EF-1): the PATCH route still answers
    with the row, so it still has to find it, and a row that is not there is still a 404."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is None:
        raise NotFound()
    label = await follow_repo.get_entity_label(db, entity_type=entity_type, entity_id=entity_id)
    return existing, label


async def unfollow(db: AsyncSession, *, user: User, entity_type: str, entity_id: str) -> None:
    """Delete the follow and commit. `NotFound` if there is none.

    Deletes the follow and nothing else — never an implicit mute. The film stays on the
    watchlist if another follow still covers it, which is the whole point of computing that set
    rather than storing it, and any mute the user has on file survives (D-40): unfollowing a
    director is not a statement about the one film of theirs the user silenced."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is None:
        raise NotFound()
    await follow_repo.delete(db, existing)
    await db.commit()
