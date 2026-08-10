"""Builders for `catalog` rows used by the ingestion tests. Films and credits are the
setup every sweep-shaped test starts from, so the two helpers live here rather than being
restated per module."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmCredit, Person


async def add_film(session: AsyncSession, tmdb_id: int, **overrides: Any) -> Film:
    """A film that is active by default — dated in the future and in production."""
    defaults: dict[str, Any] = {"status": "In Production", "title": f"Film {tmdb_id}"}
    film = Film(tmdb_id=tmdb_id, **{**defaults, **overrides})
    session.add(film)
    await session.flush()
    return film


async def add_credit(session: AsyncSession, film: Film, person_id: int, **overrides: Any) -> None:
    """One credit on `film`, creating the person if this is their first."""
    if await session.get(Person, person_id) is None:
        session.add(Person(id=person_id, name=f"Person {person_id}"))
        await session.flush()
    session.add(
        FilmCredit(
            credit_id=f"c-{film.tmdb_id}-{person_id}-{overrides.get('job') or 'cast'}",
            film_id=film.id,
            person_id=person_id,
            **overrides,
        )
    )
    await session.flush()
