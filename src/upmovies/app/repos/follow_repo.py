"""The follow graph's rows (D-10). Repo: pure DB I/O, no commits, no business rules."""

from collections import defaultdict
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from upmovies.app.models import Follow
from upmovies.catalog.models import Collection, Film, Person, ProductionCompany


class EntityLabel(NamedTuple):
    """What a follow row is called, for a caller that has the id and needs the label."""

    name: str
    image_path: str | None


# Each entity type's (primary key, name, image) columns. `entity_id` is polymorphic text and the
# four tables spell both label columns differently, so the mapping is the thing that makes one
# grouped lookup per type possible instead of a branch per row.
_LABEL_COLUMNS: dict[
    str,
    tuple[
        InstrumentedAttribute[int] | InstrumentedAttribute[UUID],
        InstrumentedAttribute[str],
        InstrumentedAttribute[str | None],
    ],
] = {
    "person": (Person.id, Person.name, Person.profile_path),
    "company": (ProductionCompany.id, ProductionCompany.name, ProductionCompany.logo_path),
    "franchise": (Collection.id, Collection.name, Collection.poster_path),
    "title": (Film.id, Film.title, Film.poster_path),
}


def _entity_key(entity_type: str, entity_id: str) -> int | UUID | None:
    """The catalog primary key `entity_id` names, or `None` when it is not of that shape.

    Person, company and franchise ids are TMDB integers; a title is a `catalog.film` UUID.
    `app.dto.normalise_entity_id` is what *should* keep anything else out of the table, but it is
    a boundary rule applied by the follow routes' request models, while `follow_service.follow`
    takes an `entity_id` straight from its caller and the imports (D-15, D-16) are about to
    become such callers. Returning `None` rather than raising is the same belt-and-braces choice
    the shape guards in `app/follow_queries.py` make, for the same reason: one bad row should
    cost that row its name, not abort the statement the user's whole list is built from."""
    try:
        return UUID(entity_id) if entity_type == "title" else int(entity_id)
    except ValueError:
        return None


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


async def get_entity_label(
    db: AsyncSession, *, entity_type: str, entity_id: str
) -> EntityLabel | None:
    """What the catalog calls this entity, or `None` if it holds no such row.

    Doubles as the existence check `app.follow` cannot make as a foreign key, because
    `entity_id` is polymorphic (see the model): a follow of a thing that does not exist would
    match nothing forever, so `follow_service` refuses to create one. Answering "what is it
    called?" and "does it exist?" with one lookup is deliberate — two would drift, and the
    create path needs both answers about the same row."""
    columns = _LABEL_COLUMNS.get(entity_type)
    if columns is None:
        raise ValueError(f"unknown entity_type {entity_type!r}")
    pk, name, image = columns
    key = _entity_key(entity_type, entity_id)
    if key is None:
        return None
    row = (await db.execute(select(name, image).where(pk == key))).first()
    return None if row is None else EntityLabel(name=row[0], image_path=row[1])


async def entity_labels(
    db: AsyncSession, follows: list[Follow]
) -> dict[tuple[str, str], EntityLabel]:
    """The labels for a whole list of follows, keyed by `(entity_type, entity_id)`.

    One grouped lookup per entity type present — at most four statements for a list of any
    length — rather than a correlated subquery or a round trip per row. A follow whose entity the
    catalog does not hold simply has no key here; the caller renders it with nulls and keeps the
    row (D-40)."""
    ids_by_type: dict[str, dict[int | UUID, str]] = defaultdict(dict)
    for follow in follows:
        if follow.entity_type not in _LABEL_COLUMNS:
            continue
        key = _entity_key(follow.entity_type, follow.entity_id)
        if key is not None:
            # Keyed by the catalog pk so the row that comes back can be matched to the follow it
            # belongs to without re-normalising: "0287" and "287" are the same person.
            ids_by_type[follow.entity_type][key] = follow.entity_id

    labels: dict[tuple[str, str], EntityLabel] = {}
    for entity_type, ids in ids_by_type.items():
        pk, name, image = _LABEL_COLUMNS[entity_type]
        rows = await db.execute(select(pk, name, image).where(pk.in_(ids)))
        for key, entity_name, image_path in rows:
            labels[(entity_type, ids[key])] = EntityLabel(name=entity_name, image_path=image_path)
    return labels
