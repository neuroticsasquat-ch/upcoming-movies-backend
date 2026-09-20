"""Mutes, and the one film lookup the watchlist service needs (D-45). Repo: pure DB I/O, no
commits, no business rules.

There is no watchlist table any more (M8, ADR-0018), so this holds what is left of one: the
`app.watchlist_dismissal` rows that subtract from the computed set, and `get_film`, which is
what turns an unknown `film_id` into a `404` before any of the rules run. The set itself is
built by `app.follow_queries` and read by the service."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import WatchlistDismissal
from upmovies.catalog.models import Film


async def get_film(db: AsyncSession, film_id: UUID) -> Film | None:
    return await db.get(Film, film_id)


async def get_films(db: AsyncSession, film_ids: set[UUID]) -> dict[UUID, Film]:
    """The films behind a list of ids, keyed by id — one query for a whole page rather than the
    round trip per row the join used to save."""
    if not film_ids:
        return {}
    rows = await db.execute(select(Film).where(Film.id.in_(film_ids)))
    return {film.id: film for film in rows.scalars().all()}


async def muted_film_ids(db: AsyncSession, user_id: UUID) -> set[UUID]:
    """Every film this user has muted, as a set to mark a list against."""
    rows = await db.execute(
        select(WatchlistDismissal.film_id).where(WatchlistDismissal.user_id == user_id)
    )
    return set(rows.scalars().all())


async def add_mute(db: AsyncSession, *, user_id: UUID, film_id: UUID) -> None:
    """Silence this film for this user. Idempotent: a mute already on file is left as it was,
    dated from the first one — muting twice is a second click, not a new decision."""
    await db.execute(
        pg_insert(WatchlistDismissal)
        .values(user_id=user_id, film_id=film_id)
        .on_conflict_do_nothing(index_elements=["user_id", "film_id"])
    )


async def delete_mute(db: AsyncSession, *, user_id: UUID, film_id: UUID) -> None:
    """Un-mute, restoring the film on every surface at once. A no-op when there is no mute."""
    await db.execute(
        delete(WatchlistDismissal).where(
            WatchlistDismissal.user_id == user_id, WatchlistDismissal.film_id == film_id
        )
    )
