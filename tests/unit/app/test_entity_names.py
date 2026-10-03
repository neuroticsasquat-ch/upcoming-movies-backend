"""`app.entity_names` (FB-15): one name lookup per entity type over a whole page of keys, never
one per row, and `None` for anything the catalog cannot name.

A recording session stands in for the database: the batching is the unit under test, and it is
a property of the statements issued, not of what Postgres returns. The integration tests on the
timeline and the digest prove the lookup against real rows."""

from typing import Any, cast

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entity_names import EntityName, entity_names
from upmovies.catalog.ref import collection_ref, company_ref, person_ref

_CATALOG = {
    "person": {525: "C. Nolan", 1032: "M. Scorsese"},
    "production_company": {508: "Regency"},
    "collection": {10: "A Collection"},
}


class _RecordingSession:
    """Answers `select(table.id, table.name).where(table.id.in_(ids))` from `_CATALOG`."""

    def __init__(self) -> None:
        self.tables: list[str] = []

    async def execute(self, stmt: Select[Any]) -> list[tuple[int, str]]:
        (table,) = stmt.get_final_froms()
        name = cast(Any, table).name
        self.tables.append(name)
        ids = set(cast(Any, stmt.whereclause).right.value)
        return [(id_, label) for id_, label in _CATALOG[name].items() if id_ in ids]


async def _names(keys: set[tuple[str, str]]) -> tuple[dict, list[str]]:
    session = _RecordingSession()
    names = await entity_names(cast(AsyncSession, session), keys)
    return names, session.tables


async def test_one_query_per_entity_type_however_many_keys():
    names, tables = await _names(
        {
            ("person", "525"),
            ("person", "1032"),
            ("person", "77"),
            ("company", "508"),
            ("franchise", "10"),
        }
    )

    assert sorted(tables) == ["collection", "person", "production_company"]
    assert names == {
        ("person", "525"): EntityName("C. Nolan", person_ref(525, "C. Nolan")),
        ("person", "1032"): EntityName("M. Scorsese", person_ref(1032, "M. Scorsese")),
        ("person", "77"): None,
        ("company", "508"): EntityName("Regency", company_ref(508, "Regency")),
        ("franchise", "10"): EntityName("A Collection", collection_ref(10, "A Collection")),
    }


async def test_a_type_with_no_keys_issues_no_query():
    _, tables = await _names({("person", "525")})

    assert tables == ["person"]


async def test_keys_the_catalog_cannot_hold_are_none_without_a_query():
    """A `title` key is not this helper's, and an id that is not a positive int32 would fail in
    the driver as it is bound — taking the page with it rather than one row's name."""
    names, tables = await _names(
        {
            ("title", "7b7a3c1e-0000-0000-0000-000000000000"),
            ("person", "x1"),
            ("person", "2147483648"),
        }
    )

    assert tables == []
    assert set(names.values()) == {None}
    assert len(names) == 3


async def test_no_keys_no_queries():
    names, tables = await _names(set())

    assert (names, tables) == ({}, [])
