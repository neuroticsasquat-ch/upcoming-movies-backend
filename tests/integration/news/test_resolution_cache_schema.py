"""`news.resolution_cache`'s widened key and the two constraints that keep it honest (EF-12).

The cache stopped being person-only when studios and franchises started resolving: `kind` is in
the primary key, and the id a row carries lives in whichever of two columns that kind names.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import Person
from upmovies.news.models import ResolutionCache

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _row(film_id, **overrides) -> ResolutionCache:
    fields: dict = {
        "source_domain": "deadline.com",
        "name_as_written": "Blumhouse",
        "film_id": film_id,
        "kind": "company",
        "entity_id": 3172,
        "confidence": 0.7,
        "resolved_at": NOW,
    }
    fields.update(overrides)
    return ResolutionCache(**fields)


async def test_one_name_on_one_film_caches_once_per_kind(session: AsyncSession):
    """ "Blumhouse" the studio and "Blumhouse" the franchise are two questions with two
    answers, and one entry could only ever hold one of them."""
    film = await add_film(session, tmdb_id=700)
    session.add(_row(film.id, kind="company", entity_id=3172))
    session.add(_row(film.id, kind="collection", entity_id=400))
    await session.flush()

    assert (
        await session.get(ResolutionCache, ("deadline.com", "Blumhouse", film.id, "company"))
    ) is not None
    assert (
        await session.get(ResolutionCache, ("deadline.com", "Blumhouse", film.id, "collection"))
    ) is not None


async def test_a_kind_outside_the_vocabulary_is_refused(session: AsyncSession):
    film = await add_film(session, tmdb_id=701)
    session.add(_row(film.id, kind="broadcaster", entity_id=1))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_person_row_may_not_carry_an_entity_id(session: AsyncSession):
    """The two id columns are exclusive by `kind`, structurally: a person row's id belongs in
    the FK-backed column, and one nobody reads back would be a silent dead end."""
    film = await add_film(session, tmdb_id=702)
    session.add(Person(id=1892, name="Chris Evans"))
    await session.flush()
    session.add(_row(film.id, kind="person", person_id=1892, entity_id=3172))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_organisation_row_may_not_carry_a_person_id(session: AsyncSession):
    film = await add_film(session, tmdb_id=703)
    session.add(Person(id=1892, name="Chris Evans"))
    await session.flush()
    session.add(_row(film.id, kind="company", person_id=1892, entity_id=3172))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_cached_negative_is_a_valid_row_for_either_kind(session: AsyncSession):
    """INV-8: nobody in TMDB matching is as much an answer as a hit is."""
    film = await add_film(session, tmdb_id=704)
    session.add(_row(film.id, kind="company", entity_id=None))
    session.add(_row(film.id, kind="person", entity_id=None, person_id=None))
    await session.flush()


async def test_kind_defaults_to_person_for_a_row_that_does_not_name_one(session: AsyncSession):
    """Every row written before EF-12 keeps the meaning it was written with, which is what
    lets the person pass read its own cache unchanged."""
    film = await add_film(session, tmdb_id=705)
    session.add(
        ResolutionCache(
            source_domain="deadline.com",
            name_as_written="Chris Evans",
            film_id=film.id,
            confidence=0.9,
            resolved_at=NOW,
        )
    )
    await session.flush()

    row = await session.get(ResolutionCache, ("deadline.com", "Chris Evans", film.id, "person"))
    assert row is not None and row.kind == "person"
