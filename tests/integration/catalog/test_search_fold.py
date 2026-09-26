"""The stored **search fold** (NEU-1469, ADR-0020): Postgres computes it on write, it agrees
with the Python-side `_normalize_query`, and each fold's pg_trgm index serves the search."""

from typing import Any

import pytest
from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Collection, Film, Person, ProductionCompany
from upmovies.public.service import (
    _name_match,
    _normalize_query,
    _searchable_query,
    _title_match,
)


async def test_stored_fold_agrees_with_the_query_fold(session):
    name = "Shōgun / Spider-Man"
    session.add(Person(id=1, name=name, original_name=None))
    session.add(Film(tmdb_id=1, title=name, original_title="기생충"))
    await session.flush()

    person = (await session.execute(select(Person.name_fold, Person.original_name_fold))).one()
    film = (await session.execute(select(Film.title_fold, Film.original_title_fold))).one()

    assert person.name_fold == _normalize_query(name) == "shogunspiderman"
    assert person.original_name_fold is None
    assert film.title_fold == "shogunspiderman"
    assert film.original_title_fold == _normalize_query("기생충") == "기생충"


async def test_stored_fold_follows_an_update(session):
    session.add(ProductionCompany(id=1, name="Studio Ghibli"))
    await session.flush()
    company = await session.get(ProductionCompany, 1)
    assert company is not None
    company.name = "Pathé"
    await session.flush()

    fold = await session.scalar(select(ProductionCompany.name_fold))
    assert fold == "pathe"


def _count(model: Any, where: Any) -> Select:
    return select(func.count()).select_from(model).where(where)


async def _plan(session: AsyncSession, stmt: Select) -> str:
    sql = stmt.compile(dialect=session.get_bind().dialect, compile_kwargs={"literal_binds": True})
    rows = await session.execute(text(f"EXPLAIN {sql}"))
    return "\n".join(r[0] for r in rows)


@pytest.mark.parametrize(
    ("statement", "indexes"),
    [
        (
            lambda nq: _count(Film, _title_match(nq)),
            [
                "ix_catalog_film_title_fold_trgm",
                "ix_catalog_film_original_title_fold_trgm",
                "ix_catalog_film_alternative_title_title_fold_trgm",
            ],
        ),
        (
            lambda nq: _count(Person, _name_match(nq, Person.name_fold, Person.original_name_fold)),
            ["ix_catalog_person_name_fold_trgm", "ix_catalog_person_original_name_fold_trgm"],
        ),
        (
            lambda nq: _count(ProductionCompany, _name_match(nq, ProductionCompany.name_fold)),
            ["ix_catalog_production_company_name_fold_trgm"],
        ),
        (
            lambda nq: _count(Collection, _name_match(nq, Collection.name_fold)),
            ["ix_catalog_collection_name_fold_trgm"],
        ),
    ],
)
async def test_search_predicates_can_use_the_trigram_indexes(session, statement, indexes):
    """Pins that the planner *sees* each index for a three-character query — not that it
    prefers it, which on a near-empty test table it never would. Seq scans are disabled so
    the plan shows whether an index can serve the predicate at all; a two-character query
    extracts no trigram and must still plan (it scans the stored column)."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))

    three = _searchable_query("Spi")
    assert three is not None
    plan = await _plan(session, statement(three))
    for index in indexes:
        assert index in plan, plan

    two = _searchable_query("sp")
    assert two is not None
    await _plan(session, statement(two))
