"""Following and unfollowing (D-10).

A follow produces timeline rows and, through `derivation_service`, the watchlist items D-13
derives from it. The rules live in a service rather than the router because the imports (D-15,
D-16) create follows too, with their own `source`, and should be calling `follow` rather than
restating the existence check — and because the derivation below has to happen for every caller
that creates a follow, not just the route."""

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.models import Follow, User
from upmovies.app.repos import follow_repo
from upmovies.app.repos.follow_repo import EntityLabel
from upmovies.app.services import derivation_service


async def follow(
    db: AsyncSession,
    *,
    user: User,
    entity_type: str,
    entity_id: str,
    source: str = "manual",
    derive: bool = True,
) -> tuple[Follow, EntityLabel | None, bool]:
    """Follow `entity_id`, commit, and say what it is called and whether the row is new.

    A new follow derives its watchlist items in the same transaction (D-13), so a user who
    follows a director sees their in-play films on the watchlist by the time the request
    answers. It shares the caller's transaction on purpose: a derivation that fails takes the
    follow with it, and the user retries one action rather than being left with a follow whose
    items silently wait for the next sweep.

    **Callers that write a watchlist item of their own must write it before the follow.** A
    title follow derives the film it names, so following first leaves a `derived_from_follow`
    item, and `watchlist_service.add` then returns that row untouched — the caller's `source`
    never lands, and the user's later removal of a film they explicitly added writes a
    dismissal (D-13). This binds D-15 and D-16: `NEU-1356-letterboxd-import.md` §3 lists the
    follow before the item for a matched `watchlist.csv` row, and that order has to flip.

    Only a *new* follow does: a second follow of the same entity has already been
    derived from, and re-deriving would at best write nothing and at worst put back a film the
    user has since dismissed — which the dismissal check would refuse anyway, making the work
    pure cost.

    `derive=False` turns that off for the imports (D-15, D-16), and only for them. An import
    writes the watchlist the user actually asked for — the films on their Letterboxd watchlist —
    and then creates hundreds of person follows in one pass; deriving from each would put every
    in-play film by every director they have ever rated four stars onto the watchlist, inline,
    as a side effect of an onboarding upload. `NEU-1356-letterboxd-import.md` §3 defers that to
    the sweep's derivation pass (NEU-1352), which runs it for every entitled user anyway. So
    this is a deferral, not a suppression: the items appear on the next sweep, and the user's
    first screen is the one they uploaded rather than one the graph inferred.

    §3 scopes that rule to *person* follows, but the imports pass `derive=False` for their title
    follows as well, and the two reasons differ. For a person it is the cost argument above. For
    a title it is that the derivation has nothing left to do: the import has already written that
    exact film's watchlist item, a line above, with its own `source` — so the pass would either
    match the row it just wrote and change nothing, or find a dismissal and refuse (D-13). Both
    outcomes are the same as skipping it, one statement cheaper.

    Idempotent: a second follow of the same entity returns the existing row untouched — its
    `source` and `created_at` record the *first* time the user showed interest, and an import
    re-run must not rewrite a manual follow as an imported one (D-15). `NotFound` if the catalog
    has no such entity: a follow of a thing that does not exist would match nothing forever."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    label = await follow_repo.get_entity_label(db, entity_type=entity_type, entity_id=entity_id)
    if existing is not None:
        return existing, label, False
    if label is None:
        raise NotFound()
    created = await follow_repo.create(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id, source=source
    )
    if derive:
        await derivation_service.derive_for_follow(
            db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
        )
    await db.commit()
    return created, label, True


async def list_follows(db: AsyncSession, *, user: User) -> list[tuple[Follow, EntityLabel | None]]:
    """Every follow with the label its entity carries in the catalog, in one grouped lookup per
    entity type. A follow the catalog cannot resolve keeps its place in the list with a `None`
    label — see `FollowOut`."""
    follows = await follow_repo.list_for_user(db, user.id)
    labels = await follow_repo.entity_labels(db, follows)
    return [(f, labels.get((f.entity_type, f.entity_id))) for f in follows]


async def unfollow(db: AsyncSession, *, user: User, entity_type: str, entity_id: str) -> None:
    """Delete the follow and commit. `NotFound` if there is none.

    Deletes the follow only. Watchlist items it derived stay (the user asked to be told about
    those films, and may have set prefs on them), and so does any dismissal."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is None:
        raise NotFound()
    await follow_repo.delete(db, existing)
    await db.commit()
