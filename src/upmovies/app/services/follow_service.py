"""Following and unfollowing (D-10). A follow is binary — there is nothing else to set (EF-1).

A follow is the only thing a user keeps (M8, ADR-0018, EF-14): it feeds the timeline, the
digest, the calendar and the iCal feed, and nothing is derived from it — creating a follow
writes one row, and every surface recomputes what that follow reaches on read.

The rules live in a service rather than the router because the imports (D-15, D-16) create
follows too, with their own `source`, and should be calling `follow` rather than restating the
existence check."""

import logging
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.follow_queries import follow_last_activity
from upmovies.app.models import Follow, User
from upmovies.app.repos import follow_repo
from upmovies.app.repos.follow_repo import EntityLabel
from upmovies.catalog.headline_release import HeadlineRelease, headline_releases

log = logging.getLogger(__name__)

FOLLOWS_WARN_THRESHOLD = 2_000
"""Follow count past which `list_follows` logs a warning: the ceiling `GET /me/follows` is
measured for, and the signal that reopens its paging design (NEU-1451, `routers/follows.py`).
A constant rather than a setting — it is documentation with a side effect, not a knob."""


class FollowRow(NamedTuple):
    """One row of the follows page: the follow, what the catalog calls it, the newest card it
    has delivered, and — for a title row only — the film's headline release (EF-14, EF-15).

    A row type rather than a widening tuple because the page's columns kept arriving: a caller
    unpacking a 2-tuple positionally is what would make each addition a rewrite of every call
    site."""

    follow: Follow
    label: EntityLabel | None
    headline: HeadlineRelease | None
    last_activity: datetime | None


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
    different sides of midnight whether a date is still upcoming is the bug that buys.

    Unbounded by decision: the cost and the ceiling are in `routers/follows.py::list_follows`,
    and past `FOLLOWS_WARN_THRESHOLD` rows this logs once per call so the ceiling is a signal."""
    follows = await follow_repo.list_for_user(db, user.id)
    if len(follows) > FOLLOWS_WARN_THRESHOLD:
        log.warning(
            "user_id=%s has %d follows, past the %d-row ceiling GET /me/follows is measured "
            "for (NEU-1451)",
            user.id,
            len(follows),
            FOLLOWS_WARN_THRESHOLD,
        )
    labels = await follow_repo.entity_labels(db, [(f.entity_type, f.entity_id) for f in follows])
    film_ids = [_film_id(f) for f in follows]
    headlines = await headline_releases(
        db, [i for i in film_ids if i is not None], today=datetime.now(tz=UTC).date()
    )
    activity = await _last_activity(db, user_id=user.id)
    return [
        FollowRow(
            follow=f,
            label=labels.get((f.entity_type, f.entity_id)),
            headline=None if film_id is None else headlines.get(film_id),
            last_activity=activity.get((f.entity_type, f.entity_id)),
        )
        for f, film_id in zip(follows, film_ids, strict=True)
    ]


async def _last_activity(
    db: AsyncSession, *, user_id: UUID, only: tuple[str, str] | None = None
) -> dict[tuple[str, str], datetime]:
    """`{(entity_type, entity_id): last_activity_at}` for this user's follows, or for the one
    `only` names (EF-15).

    **One statement whichever it is** (`follow_queries.follow_last_activity`), which is the
    whole reason the column is computed in SQL: the list route needs it for every row before it
    can draw the first one, and a query per row would be a round trip per follow on a page an
    imported library fills with hundreds.

    A follow that has delivered nothing is absent from the mapping, and the caller's `.get`
    turns that into the null `FollowOut` documents. Missing and NULL are the same answer here —
    "nothing yet" — so there is nothing for an outer join to add."""
    rows = (await db.execute(follow_last_activity(user_id, only=only))).all()
    return {(entity_type, entity_id): at for entity_type, entity_id, at in rows}


async def last_activity_for(db: AsyncSession, follow: Follow) -> datetime | None:
    """The newest card a single follow has delivered, for the three single-row routes.

    What `headline_for` is to the date column: the write responses have to carry the same
    fields `GET /me/follows` does (EF-15), because the client reconciles its cache from them —
    a follow button that answered with a null `last_activity_at` while the list answered with a
    real one would sort the row to the bottom until the next fetch.

    Reads the same builder the list route batches, narrowed to one row, so a follow's date
    cannot depend on which route reported it."""
    key = (follow.entity_type, follow.entity_id)
    return (await _last_activity(db, user_id=follow.user_id, only=key)).get(key)


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
