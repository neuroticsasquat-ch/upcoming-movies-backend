"""Watchlist items and dismissals (D-13, D-14). Repo: pure DB I/O, no commits, no business
rules."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import WatchlistDismissal, WatchlistItem
from upmovies.catalog.models import Film


async def get(db: AsyncSession, *, user_id: UUID, film_id: UUID) -> WatchlistItem | None:
    return await db.get(WatchlistItem, (user_id, film_id))


async def get_film(db: AsyncSession, film_id: UUID) -> Film | None:
    return await db.get(Film, film_id)


async def create(
    db: AsyncSession, *, user_id: UUID, film_id: UUID, source: str, alert_prefs: list[str]
) -> WatchlistItem:
    item = WatchlistItem(user_id=user_id, film_id=film_id, source=source, alert_prefs=alert_prefs)
    db.add(item)
    await db.flush()
    return item


async def list_for_user(db: AsyncSession, user_id: UUID) -> list[tuple[WatchlistItem, Film]]:
    """Each item with its film, newest first — one query, because the row is rendered with the
    film's title and poster and a query per item would be the N+1 this join exists to avoid."""
    rows = await db.execute(
        select(WatchlistItem, Film)
        .join(Film, Film.id == WatchlistItem.film_id)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.created_at.desc(), Film.title, Film.id)
    )
    return [(item, film) for item, film in rows.tuples().all()]


async def set_alert_prefs(db: AsyncSession, item: WatchlistItem, *, alert_prefs: list[str]) -> None:
    """Replace the prefs on the loaded model. Caller commits."""
    item.alert_prefs = alert_prefs


async def delete(db: AsyncSession, item: WatchlistItem) -> None:
    await db.delete(item)
    await db.flush()


async def add_dismissal(db: AsyncSession, *, user_id: UUID, film_id: UUID) -> None:
    """Record that this user refused this film's derivation. Idempotent: a dismissal already on
    file is left as it was, dated from the first refusal."""
    await db.execute(
        pg_insert(WatchlistDismissal)
        .values(user_id=user_id, film_id=film_id)
        .on_conflict_do_nothing(index_elements=["user_id", "film_id"])
    )
