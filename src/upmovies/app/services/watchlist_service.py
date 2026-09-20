"""The watchlist's rules (D-42, D-45): reading the computed set, and the two verbs that change
what it holds.

**Nothing here stores a watchlist.** The set is `follow_queries.watchlist_film_ids` — the films
this user's follows cover, minus the ones they have muted — recomputed on every read, so a
coverage narrowed, a follow deleted or a film ageing out of the alert window takes effect
everywhere at once (ADR-0018). What the two verbs write is a **follow** and a **mute**:

- **want** (`POST`) — un-mute, and create a manual title follow if nothing else covers the film.
  The user asked for this film, so they get a row of their own that says so, rather than a
  dependency on the person follow that happened to reach it.
- **stop** (`DELETE`) — delete the direct title follow, and if anything *else* still covers the
  film, mute it. Both halves are needed and neither is sufficient: deleting only the follow
  would leave the film on the list through the director who also reaches it, and muting only
  would leave a follow the user no longer wants behind the silence.

Both answer the *resulting state* rather than what they did to reach it, which is what lets
NEU-1405 reconcile its cache from one response.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound, NothingToStop
from upmovies.app.follow_queries import covered_film_ids, covering_follows
from upmovies.app.models import User
from upmovies.app.repos import follow_repo, watchlist_repo
from upmovies.app.services import follow_service
from upmovies.catalog.headline_release import HeadlineRelease, headline_releases
from upmovies.catalog.models import Film
from upmovies.config import Settings, get_settings


@dataclass(frozen=True)
class Cover:
    """One follow that covers a film, with what the catalog calls it. `name` is `None` for a
    follow the catalog cannot resolve (D-40, as `FollowOut`)."""

    entity_type: str
    entity_id: str
    name: str | None


@dataclass(frozen=True)
class WatchlistEntry:
    """One film on the computed watchlist, assembled from the follows that cover it."""

    film: Film
    headline: HeadlineRelease | None
    covered_by: tuple[Cover, ...]
    followed: bool
    muted: bool
    created_at: datetime


def _window() -> tuple[date, int]:
    """The two arguments every coverage query takes, resolved once per request.

    Request-time entry points resolve their own clock and settings — `public.service
    .get_timeline` does the same — because the alternative is every route that touches the
    watchlist assembling the same values. The batch passes are handed them instead, since
    a pass fixes one `today` for its whole run."""
    settings: Settings = get_settings()
    return (datetime.now(tz=UTC).date(), settings.provider_poll_max_age_days)


def _cover_sort_key(cover: tuple[str, str, datetime]) -> tuple[int, datetime, str, str]:
    """Direct title follow first, then the covering follows oldest first (D-1414.5).

    The direct follow leads because it is the answer to "why is this here?" whenever it exists:
    the user put it there. Everything else is ordered by when the user started following it, so
    the "via Christopher Nolan" line names the oldest standing interest rather than whichever
    row the union happened to emit first."""
    entity_type, entity_id, created_at = cover
    return (0 if entity_type == "title" else 1, created_at, entity_type, entity_id)


async def _entries(
    db: AsyncSession, *, user: User, film_id: UUID | None = None
) -> list[WatchlistEntry]:
    """The computed watchlist, or just one film of it, newest first.

    One query for the (film, follow) pairs, one for the films, one for the headline releases
    and at most four for the covering entities' names — a fixed number of statements for a list
    of any length, which is what the old join bought and what this has to keep.

    Muted films are **included and marked**, not dropped: the list is where a mute is undone.
    """
    today, max_age_days = _window()
    rows = await db.execute(
        covering_follows(
            user_id=user.id,
            today=today,
            max_age_days=max_age_days,
            film_id=film_id,
        )
    )
    covers: dict[UUID, list[tuple[str, str, datetime]]] = {}
    for row in rows:
        covers.setdefault(row.film_id, []).append((row.entity_type, row.entity_id, row.created_at))
    if not covers:
        return []

    films = await watchlist_repo.get_films(db, set(covers))
    muted = await watchlist_repo.muted_film_ids(db, user.id)
    headlines = await headline_releases(db, list(films), today=today)
    labels = await follow_repo.entity_labels(
        db,
        [
            (entity_type, entity_id)
            for pairs in covers.values()
            for entity_type, entity_id, _ in pairs
        ],
    )

    entries: list[WatchlistEntry] = []
    for covered_film_id, pairs in covers.items():
        film = films.get(covered_film_id)
        if film is None:
            # The pair named a film the catalog no longer holds. Films are never deleted
            # (spec §4.4), so this is unreachable in practice — and skipping beats raising
            # for a list the user is looking at.
            continue
        ordered = sorted(pairs, key=_cover_sort_key)
        label_of = labels.get
        entries.append(
            WatchlistEntry(
                film=film,
                headline=headlines.get(film.id),
                covered_by=tuple(
                    Cover(
                        entity_type=entity_type,
                        entity_id=entity_id,
                        name=(
                            None
                            if (label := label_of((entity_type, entity_id))) is None
                            else label.name
                        ),
                    )
                    for entity_type, entity_id, _ in ordered
                ),
                followed=any(entity_type == "title" for entity_type, _, _ in pairs),
                muted=film.id in muted,
                # The earliest covering follow: when this film started being covered, which is
                # what the list sorts on. A film reached by a director followed last year and
                # a company followed today has been on the way since last year.
                created_at=min(created_at for _, _, created_at in pairs),
            )
        )
    entries.sort(key=lambda e: (-e.created_at.timestamp(), e.film.title, str(e.film.id)))
    return entries


async def _is_covered(db: AsyncSession, *, user: User, film_id: UUID) -> bool:
    """Whether **anything** this user follows covers this film, mutes ignored.

    Mutes are deliberately not subtracted: this is what decides between muting a film and
    answering that there is nothing left to mute, and a film the user has already silenced is
    still covered."""
    today, max_age_days = _window()
    covered = covered_film_ids(
        user_id=user.id,
        today=today,
        max_age_days=max_age_days,
    ).where(Film.id == film_id)
    return bool(await db.scalar(select(covered.exists())))


async def list_items(db: AsyncSession, *, user: User) -> list[WatchlistEntry]:
    """Every film this user's follows cover, muted ones included and marked."""
    return await _entries(db, user=user)


async def _one(db: AsyncSession, *, user: User, film_id: UUID) -> WatchlistEntry | None:
    entries = await _entries(db, user=user, film_id=film_id)
    return entries[0] if entries else None


async def want(db: AsyncSession, *, user: User, film_id: UUID) -> WatchlistEntry:
    """**Want** this film: un-mute it, and follow the title if nothing already covers it.
    Commits and answers the resulting item. `NotFound` if there is no such film.

    Idempotent, and deliberately silent about which of the two it did: wanting a film a
    followed director already covers writes nothing but the un-mute, and answers the same item
    a second press would. What it does *not* do is create a title follow beside a covering one
    — the user would then be following the film twice over, and stopping it once would not stop
    it.

    A title follow covers its film in any state, so wanting a film that came out years ago
    yields an item rather than an empty answer, which is what the import path relies on."""
    film = await watchlist_repo.get_film(db, film_id)
    if film is None:
        raise NotFound()
    await watchlist_repo.delete_mute(db, user_id=user.id, film_id=film_id)
    if not await _is_covered(db, user=user, film_id=film_id):
        await follow_service.follow(db, user=user, entity_type="title", entity_id=str(film_id))
    await db.commit()
    entry = await _one(db, user=user, film_id=film_id)
    assert entry is not None  # the film is covered by the follow above, or was already
    return entry


async def stop(db: AsyncSession, *, user: User, film_id: UUID) -> WatchlistEntry | None:
    """**Stop** this film: delete the direct title follow, then mute it if anything else still
    covers it. Commits. Answers the muted item, or `None` when nothing covers the film any more
    and so there is no item to answer with (the route's `204`).

    `NotFound` for an unknown film; `NothingToStop` for a real film nothing covers and nobody
    has muted. Two `404`s with different details, because "that is not a film" and "that film
    was never on your list" are different things to be told — and the second is a different
    answer again from "stopped, and now there is nothing left", which is the `None` above.

    The mute is written *after* the follow is deleted, and only if something survives it: a
    film the user was only following directly leaves the list outright, and writing a mute for
    it would be a permanent record of a film they merely removed — which is the thing D-40
    keeps out of an unfollow."""
    film = await watchlist_repo.get_film(db, film_id)
    if film is None:
        raise NotFound()
    direct = await follow_repo.get(db, user_id=user.id, entity_type="title", entity_id=str(film_id))
    if direct is not None:
        await follow_repo.delete(db, direct)
    if await _is_covered(db, user=user, film_id=film_id):
        await watchlist_repo.add_mute(db, user_id=user.id, film_id=film_id)
        await db.commit()
        return await _one(db, user=user, film_id=film_id)
    muted = film_id in await watchlist_repo.muted_film_ids(db, user.id)
    if direct is None and not muted:
        await db.rollback()
        raise NothingToStop()
    await db.commit()
    return None
