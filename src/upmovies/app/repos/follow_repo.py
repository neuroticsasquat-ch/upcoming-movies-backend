"""The follow graph's rows (D-10). Repo: pure DB I/O, no commits, no business rules."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import Follow
from upmovies.catalog.models import Collection, Film, Person, ProductionCompany


async def get(
    db: AsyncSession, *, user_id: UUID, entity_type: str, entity_id: str
) -> Follow | None:
    return await db.get(Follow, (user_id, entity_type, entity_id))


async def create(
    db: AsyncSession, *, user_id: UUID, entity_type: str, entity_id: str, source: str
) -> Follow:
    follow = Follow(user_id=user_id, entity_type=entity_type, entity_id=entity_id, source=source)
    db.add(follow)
    await db.flush()
    return follow


async def list_for_user(db: AsyncSession, user_id: UUID) -> list[Follow]:
    rows = await db.execute(
        select(Follow)
        .where(Follow.user_id == user_id)
        .order_by(Follow.created_at.desc(), Follow.entity_type, Follow.entity_id)
    )
    return list(rows.scalars().all())


async def delete(db: AsyncSession, follow: Follow) -> None:
    await db.delete(follow)
    await db.flush()


async def entity_exists(db: AsyncSession, *, entity_type: str, entity_id: str) -> bool:
    """Whether the catalog holds the thing this follow would point at.

    The check `app.follow` cannot make as a foreign key, because `entity_id` is polymorphic
    (see the model). `entity_id` arrives already normalised by the DTO, so the casts are safe."""
    match entity_type:
        case "person":
            return await db.get(Person, int(entity_id)) is not None
        case "company":
            return await db.get(ProductionCompany, int(entity_id)) is not None
        case "franchise":
            return await db.get(Collection, int(entity_id)) is not None
        case "title":
            return await db.get(Film, UUID(entity_id)) is not None
    raise ValueError(f"unknown entity_type {entity_type!r}")
