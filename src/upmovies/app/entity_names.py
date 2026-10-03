"""What a person, studio or franchise follow is called, and where its page lives (FB-15).

The timeline names the entity each of its entity rows came through (`FeedVia`), and the digest
heads its entity rows the same way; both start from `follow_attribution_pairs`-shaped
`(entity_type, entity_id)` keys and need the same two things back — the catalog's current name
and the canonical ref the entity page's route takes. One lookup per entity type over a whole
page of keys, never one per row.

The follow graph's words, not the catalog's: `company` is `catalog.production_company` and
`franchise` is `catalog.collection` (CONTEXT.md **Franchise**). The refs come from the helpers
the entity pages' canonical redirects use, so a link built here never 301s.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Collection, Person, ProductionCompany
from upmovies.catalog.ref import collection_ref, company_ref, person_ref

ENTITY_TYPES: tuple[str, ...] = ("person", "company", "franchise")
"""The entity follow types, in the order the timeline's blocks and the digest list them."""

_TABLES: dict[str, type[Person] | type[ProductionCompany] | type[Collection]] = {
    "person": Person,
    "company": ProductionCompany,
    "franchise": Collection,
}
_REFS: dict[str, Callable[[int, str], str]] = {
    "person": person_ref,
    "company": company_ref,
    "franchise": collection_ref,
}

# The catalog's keys are `Integer`; an id past int32 would fail in the driver as it is bound,
# taking the whole page with it rather than one row's name (`follow_repo._entity_key`).
_INT32_MAX = 2**31 - 1


@dataclass(frozen=True)
class EntityName:
    name: str
    ref: str


def _catalog_key(entity_id: str) -> int | None:
    if not entity_id.isdigit():
        return None
    key = int(entity_id)
    return key if 0 < key <= _INT32_MAX else None


async def entity_names(
    session: AsyncSession, keys: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], EntityName | None]:
    """Every `(entity_type, entity_id)` asked for, mapped to its name and ref — `None` for an
    entity the catalog cannot name (a person purged from TMDB, an id of the wrong shape, a
    `title` key, which is not this helper's).

    One `SELECT` per entity type present in `keys`, however many keys of it there are.
    """
    asked = set(keys)
    wanted: dict[str, set[int]] = {}
    for entity_type, entity_id in asked:
        key = _catalog_key(entity_id)
        if entity_type in _TABLES and key is not None:
            wanted.setdefault(entity_type, set()).add(key)

    found: dict[tuple[str, int], EntityName] = {}
    for entity_type, ids in wanted.items():
        table = _TABLES[entity_type]
        for id_, name in await session.execute(
            select(table.id, table.name).where(table.id.in_(ids))
        ):
            found[(entity_type, id_)] = EntityName(name=name, ref=_REFS[entity_type](id_, name))

    def _resolve(entity_type: str, entity_id: str) -> EntityName | None:
        key = _catalog_key(entity_id)
        return None if key is None else found.get((entity_type, key))

    return {pair: _resolve(*pair) for pair in asked}
