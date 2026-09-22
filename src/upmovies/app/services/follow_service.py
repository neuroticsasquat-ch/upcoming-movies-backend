"""Following and unfollowing (D-10). A follow is binary — there is nothing else to set (EF-1).

A follow is the only thing a user keeps (M8, ADR-0018, EF-14): it feeds the timeline, the
alerts, the calendar and the iCal feed, and nothing is derived from it — creating a follow
writes one row, and every surface recomputes what that follow reaches on read.

The rules live in a service rather than the router because the imports (D-15, D-16) create
follows too, with their own `source`, and should be calling `follow` rather than restating the
existence check."""

from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.models import Follow, User
from upmovies.app.repos import follow_repo
from upmovies.app.repos.follow_repo import EntityLabel
from upmovies.catalog.headline_release import HeadlineRelease, headline_releases


class FollowRow(NamedTuple):
    """One row of the follows page: the follow, what the catalog calls it, and — for a title
    row only — the film's headline release (EF-14, EF-15).

    A row type rather than a widening tuple because the page's columns are still arriving:
    NEU-1440 hangs `last_activity_at` off the same rows, and a caller unpacking a 2-tuple
    positionally is what makes each addition a rewrite of every call site."""

    follow: Follow
    label: EntityLabel | None
    headline: HeadlineRelease | None


async def follow(
    db: AsyncSession,
    *,
    user: User,
    entity_type: str,
    entity_id: str,
    source: str = "manual",
) -> tuple[Follow, EntityLabel | None, bool]:
    """Follow `entity_id`, commit, and say what it is called and whether the row is new.

    Idempotent: a second follow of the same entity returns the existing row **untouched**. Its
    `source` and `created_at` record the *first* time the user showed interest, and an import
    re-run must not rewrite a manual follow as an imported one (D-15).

    `NotFound` if the catalog has no such entity: a follow of a thing that does not exist would
    match nothing forever."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    label = await follow_repo.get_entity_label(db, entity_type=entity_type, entity_id=entity_id)
    if existing is not None:
        return existing, label, False
    if label is None:
        raise NotFound()
    created = await follow_repo.create(
        db,
        user_id=user.id,
        entity_type=entity_type,
        entity_id=entity_id,
        source=source,
    )
    await db.commit()
    return created, label, True


async def list_follows(db: AsyncSession, *, user: User) -> list[FollowRow]:
    """Every follow with the label its entity carries in the catalog, in one grouped lookup per
    entity type. A follow the catalog cannot resolve keeps its place in the list with a `None`
    label — see `FollowOut`.

    Title rows also carry the film's headline release, from the one batch query every film row
    on this site reads (`catalog.headline_release`), so the follows page can show dates without
    a request per row (EF-14). One statement pair for the whole list, and none at all for a user
    who follows no films.

    `today` is resolved here, Python-side and once for the whole response, the way every
    request-time entry point in this codebase does it: two rows of one list deciding on
    different sides of midnight whether a date is still upcoming is the bug that buys."""
    follows = await follow_repo.list_for_user(db, user.id)
    labels = await follow_repo.entity_labels(db, [(f.entity_type, f.entity_id) for f in follows])
    film_ids = [_film_id(f) for f in follows]
    headlines = await headline_releases(
        db, [i for i in film_ids if i is not None], today=datetime.now(tz=UTC).date()
    )
    return [
        FollowRow(
            follow=f,
            label=labels.get((f.entity_type, f.entity_id)),
            headline=None if film_id is None else headlines.get(film_id),
        )
        for f, film_id in zip(follows, film_ids, strict=True)
    ]


async def headline_for(db: AsyncSession, follow: Follow) -> HeadlineRelease | None:
    """The headline release of the film a single follow names, or `None` for any other type.

    What the three single-row routes need so their `FollowOut` carries the same fields the list
    route's does (EF-14). A follow button that answered with a null date while `GET /me/follows`
    answered with a real one would hand the client two truths about one row, and reconciling a
    cache from the write response — which is what the film page does — would then show "No date
    yet" until the next fetch.

    One extra statement pair on a write, and none at all for a person, studio or franchise
    follow. `list_follows` batches instead, because it has a whole page of rows to resolve."""
    film_id = _film_id(follow)
    if film_id is None:
        return None
    headlines = await headline_releases(db, [film_id], today=datetime.now(tz=UTC).date())
    return headlines.get(film_id)


def _film_id(follow: Follow) -> UUID | None:
    """The `catalog.film` id a title row names, or `None` for any other type — and for a title
    row whose `entity_id` is not a UUID.

    Parsed once per row and carried, rather than tested here and parsed again at the point of
    use: the second parse is where the guard gets forgotten, and forgetting it 500s the whole
    list on behalf of one malformed row. The same belt and braces `app/follow_queries.py`'s
    shape guards and `follow_repo._entity_key` apply, for the same reason — `entity_id` is
    polymorphic text and only the routes' request models normalise it."""
    if follow.entity_type != "title":
        return None
    try:
        return UUID(follow.entity_id)
    except ValueError:
        return None


async def get_follow(
    db: AsyncSession, *, user: User, entity_type: str, entity_id: str
) -> tuple[Follow, EntityLabel | None]:
    """One follow and its label, or `NotFound`. Reads only — nothing is committed.

    What is left of `set_coverage` now the tier is gone (EF-1): the PATCH route still answers
    with the row, so it still has to find it, and a row that is not there is still a 404."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is None:
        raise NotFound()
    label = await follow_repo.get_entity_label(db, entity_type=entity_type, entity_id=entity_id)
    return existing, label


async def unfollow(db: AsyncSession, *, user: User, entity_type: str, entity_id: str) -> None:
    """Delete the follow and commit. `NotFound` if there is none.

    Deletes the follow and nothing else (D-40). There is no longer anything else to delete:
    the mute went with the watchlist it corrected (EF-14), and unfollowing **is** the
    correction now — a title follow is the only thing that puts a film on the user's list, so
    removing it removes the film, and removing a person follow takes that person's cards with
    it and touches no film at all."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is None:
        raise NotFound()
    await follow_repo.delete(db, existing)
    await db.commit()
