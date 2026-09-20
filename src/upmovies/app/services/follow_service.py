"""Following, unfollowing, and a person follow's coverage (D-10, D-43).

A follow is the only thing a user keeps (M8, ADR-0018): it feeds the timeline *and* the
alerts, and the watchlist is a query over it (`app.follow_queries`). Nothing is derived from
it any more — creating a follow writes one row, and every surface recomputes what that follow
reaches on read.

The rules live in a service rather than the router because the imports (D-15, D-16) create
follows too, with their own `source`, and should be calling `follow` rather than restating the
existence check."""

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import NotFound
from upmovies.app.models import DEFAULT_COVERAGE, Follow, User
from upmovies.app.repos import follow_repo
from upmovies.app.repos.follow_repo import EntityLabel


async def follow(
    db: AsyncSession,
    *,
    user: User,
    entity_type: str,
    entity_id: str,
    source: str = "manual",
    coverage: str | None = None,
) -> tuple[Follow, EntityLabel | None, bool]:
    """Follow `entity_id`, commit, and say what it is called and whether the row is new.

    `coverage` is the person tier (D-43); `None` takes the column's default. It is written on
    every row, whatever the type, so the column can be NOT NULL and the coverage query can read
    it without a CASE — for the other three types it is stored and never read.

    Idempotent: a second follow of the same entity returns the existing row **untouched**,
    coverage included. Its `source` and `created_at` record the *first* time the user showed
    interest, an import re-run must not rewrite a manual follow as an imported one (D-15), and
    changing a tier is `set_coverage`'s job — a follow button pressed again is not a request to
    reset what the user chose on the person page.

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
        coverage=DEFAULT_COVERAGE if coverage is None else coverage,
    )
    await db.commit()
    return created, label, True


async def list_follows(db: AsyncSession, *, user: User) -> list[tuple[Follow, EntityLabel | None]]:
    """Every follow with the label its entity carries in the catalog, in one grouped lookup per
    entity type. A follow the catalog cannot resolve keeps its place in the list with a `None`
    label — see `FollowOut`."""
    follows = await follow_repo.list_for_user(db, user.id)
    labels = await follow_repo.entity_labels(db, [(f.entity_type, f.entity_id) for f in follows])
    return [(f, labels.get((f.entity_type, f.entity_id))) for f in follows]


async def set_coverage(
    db: AsyncSession, *, user: User, entity_type: str, entity_id: str, coverage: str
) -> tuple[Follow, EntityLabel | None]:
    """Set which of a followed person's credits alert, and commit (D-43). `NotFound` if there
    is no such follow.

    Takes effect on the next read of every surface at once, because nothing stores what the
    follow covers: narrowing `all` to `lead` drops the films it was only reaching through a
    fourth-billed credit from the watchlist, the calendar and the poll set, which is precisely
    the reconciliation a materialised watchlist could never do.

    The caller is what refuses this for a non-person follow (`422 coverage_not_applicable`),
    where the request model already knows the type. The value is stored on those rows and never
    read, so writing one would be neither wrong nor meaningful — and answering as though it had
    done something would be the lie."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is None:
        raise NotFound()
    await follow_repo.set_coverage(db, existing, coverage=coverage)
    await db.commit()
    label = await follow_repo.get_entity_label(db, entity_type=entity_type, entity_id=entity_id)
    return existing, label


async def unfollow(db: AsyncSession, *, user: User, entity_type: str, entity_id: str) -> None:
    """Delete the follow and commit. `NotFound` if there is none.

    Deletes the follow and nothing else — never an implicit mute. The film stays on the
    watchlist if another follow still covers it, which is the whole point of computing that set
    rather than storing it, and any mute the user has on file survives (D-40): unfollowing a
    director is not a statement about the one film of theirs the user silenced."""
    existing = await follow_repo.get(
        db, user_id=user.id, entity_type=entity_type, entity_id=entity_id
    )
    if existing is None:
        raise NotFound()
    await follow_repo.delete(db, existing)
    await db.commit()
