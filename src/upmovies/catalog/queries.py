"""Shared query predicates over `catalog.film`."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmFieldChange


def active_film_clause(*, today: date, excluded_statuses: frozenset[str]) -> ColumnElement[bool]:
    """WHERE predicate selecting films still in play (not released/canceled).

    A film is INACTIVE when ``release_date < today`` OR ``status`` is in
    ``excluded_statuses``; this returns the negation. The NULL guards keep undated
    films and films with an unknown status in the active set — without them SQL's
    ``NULL NOT IN (...)`` evaluates to NULL and would wrongly drop those rows.
    """
    return and_(
        or_(Film.release_date.is_(None), Film.release_date >= today),
        or_(Film.status.is_(None), Film.status.not_in(excluded_statuses)),
    )


async def field_changed_at(session: AsyncSession, film_id: UUID, field: str) -> datetime | None:
    """The most recent time `field` changed on this film, or None if it has never
    changed since insert (the trigger is UPDATE-only). Callers treat None as
    'known since at least `film.created_at`'."""
    stmt = (
        select(FilmFieldChange.changed_at)
        .where(FilmFieldChange.film_id == film_id, FilmFieldChange.field == field)
        .order_by(FilmFieldChange.changed_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
