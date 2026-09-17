"""The watchlist's rules (D-13, D-14): what a manual add does to an existing row, and what a
removal leaves behind.

In a service rather than the router for the same reason as `follow_service`: the imports
(D-15, D-16) and the derivation pass (NEU-1352) write these rows too, and the one rule that
matters most — a removed derived item is a *dismissal* — must not depend on which caller did
the removing."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.models import DEFAULT_ALERT_PREFS, User, WatchlistItem
from upmovies.app.repos import watchlist_repo
from upmovies.catalog.models import Film


async def add(
    db: AsyncSession,
    *,
    user: User,
    film_id: UUID,
    alert_prefs: Sequence[str] | None = None,
    source: str = "manual",
) -> tuple[WatchlistItem, Film, bool]:
    """Put `film_id` on the watchlist, commit, and say whether the row is new.

    Idempotent, and an existing row is returned untouched — prefs included, because a second
    add is a double click or an import re-run, not a request to reset what the user chose; the
    PATCH is how prefs change. A dismissal on file does not block this: it binds the derivation
    only, and a user adding a film by hand has overruled it. `NotFound` if there is no such
    film."""
    film = await watchlist_repo.get_film(db, film_id)
    if film is None:
        raise NotFound()
    existing = await watchlist_repo.get(db, user_id=user.id, film_id=film_id)
    if existing is not None:
        return existing, film, False
    created = await watchlist_repo.create(
        db,
        user_id=user.id,
        film_id=film_id,
        source=source,
        alert_prefs=list(DEFAULT_ALERT_PREFS if alert_prefs is None else alert_prefs),
    )
    await db.commit()
    return created, film, True


async def list_items(db: AsyncSession, *, user: User) -> list[tuple[WatchlistItem, Film]]:
    return await watchlist_repo.list_for_user(db, user.id)


async def set_alert_prefs(
    db: AsyncSession, *, user: User, film_id: UUID, alert_prefs: Sequence[str]
) -> tuple[WatchlistItem, Film]:
    """Replace the item's prefs and commit. `NotFound` if the film is not on the watchlist."""
    item = await watchlist_repo.get(db, user_id=user.id, film_id=film_id)
    if item is None:
        raise NotFound()
    film = await watchlist_repo.get_film(db, film_id)
    assert film is not None  # the item's FK guarantees it
    await watchlist_repo.set_alert_prefs(db, item, alert_prefs=list(alert_prefs))
    await db.commit()
    return item, film


async def remove(db: AsyncSession, *, user: User, film_id: UUID) -> None:
    """Take the film off the watchlist and commit. `NotFound` if it was not on it.

    If the follow graph put the item there, the removal is a dismissal (D-13): a row is written
    that stops the derivation pass from adding the film again, on this follow or any later one.
    A manual item leaves nothing behind — the user chose it and un-chose it, and the derivation
    was never involved."""
    item = await watchlist_repo.get(db, user_id=user.id, film_id=film_id)
    if item is None:
        raise NotFound()
    if item.source == "derived_from_follow":
        await watchlist_repo.add_dismissal(db, user_id=user.id, film_id=film_id)
    await watchlist_repo.delete(db, item)
    await db.commit()
