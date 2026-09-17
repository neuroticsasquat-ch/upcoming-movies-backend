"""Derived watchlist items: the follow graph putting films on a user's watchlist (D-13).

Two callers, one rule. `derive_for_follow` runs synchronously when a follow is created, scoped
to that follow; `derive_for_user` runs over everything a user follows and is what the sweep's
derivation phase (`ingest.sweep.derivation_phase`) calls for every entitled user once the
credits pass has written the day's new credits. Entitlement is the caller's business, not this
module's: the follow routes are already gated (D-39), and the batch half filters unentitled
users in its own query, where the users are being selected.

**The cut is not the timeline's.** D-11 gives a person follow every *seed-grade* credit
(`catalog.seed_grade`: director, writer, top-5 billed); D-13 gives the watchlist **director or
top-3 billing** only. A watchlist item is the one row in the system that produces a push, so it
is deliberately the narrower claim — a 4th-billed casting or a writing credit is worth a
timeline row, not a notification. The other three entity types match as they do everywhere, and
**every** branch here is additionally restricted to films in play, which is the second
difference from D-11: there the in-play term is the person branch's alone, because following a
title is a request for that specific film whether or not it has come out. A watchlist exists to
say *this is coming*, so a released or cancelled film has nothing left to alert on.

**One statement, not a read then a write.** The exclusions — an item already on the list, a
dismissal on file (D-13) — are `NOT EXISTS` terms inside the `INSERT ... SELECT`, so the pass
cannot race itself between deciding and inserting, and `ON CONFLICT DO NOTHING` catches the case
where two of them (a follow POST and a sweep pass) decide at the same instant anyway. That is
what makes this idempotent in the sense the ticket asks for: running it twice adds nothing, and
an existing item is never rewritten — its `source` and `alert_prefs` are the user's, whoever put
it there first.

Neither entry point commits: callers own the transaction (CLAUDE.md), which is what lets the
follow route write the follow and its derived items as one unit."""

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, exists, literal, or_, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.follow_queries import followed_film_uuids, followed_tmdb_ids
from upmovies.app.models import WatchlistDismissal, WatchlistItem
from upmovies.catalog.models import Film, FilmCredit, FilmProductionCompany
from upmovies.catalog.queries import in_play_clause
from upmovies.catalog.seed_grade import DIRECTOR_JOB
from upmovies.config import get_settings

DERIVED_SOURCE = "derived_from_follow"
"""`watchlist_item.source` for a row the follow graph wrote — the value that makes a later
removal a dismissal rather than a plain delete (D-13)."""

WATCHLIST_TOP_BILLED_ORDER = 3
"""D-13's billing cut, on TMDB's 0-indexed `order`: slots 0, 1 and 2.

Not `catalog.seed_grade.TOP_BILLED_ORDER`, which is 5, and not a candidate for being unified
with it. The two answer different questions and the numbers are load-bearing in opposite
directions: seed grade decides whose filmography the sweep *enumerates*, where the measurement
at NEU-1090 shows cutting to 3 discards demonstrably-shooting films at 14:1, while this decides
whose casting is worth a **push**, where 5 would notify a user about a supporting role they did
not follow the person for."""


def watchlist_credit_clause() -> ColumnElement[bool]:
    """WHERE predicate over `catalog.film_credit` selecting D-13's credits: a director credit,
    or a cast credit billed in the top 3.

    `credit_order IS NOT NULL` is not redundant beside `< 3`: TMDB leaves `order` off the long
    tail of a cast list, and NULL must read as "unbilled", not as slot 0."""
    return or_(
        and_(FilmCredit.credit_type == "crew", FilmCredit.job == DIRECTOR_JOB),
        and_(
            FilmCredit.credit_type == "cast",
            FilmCredit.credit_order.is_not(None),
            FilmCredit.credit_order < WATCHLIST_TOP_BILLED_ORDER,
        ),
    )


def derivable_film_ids(
    *,
    user_id: UUID,
    today: date,
    excluded_statuses: frozenset[str],
    only: tuple[str, str] | None = None,
) -> Select[tuple[UUID]]:
    """`SELECT film.id` for every film this user's follows would add to their watchlist right
    now (D-13) — matched, in play, and neither already on the list nor dismissed.

    `only` narrows the graph to one `(entity_type, entity_id)`: the follow-creation half derives
    from the follow just made, and the branches for the other three types are dropped from the
    statement rather than fed an empty id list, so a POST costs one branch's work instead of
    four.

    A query builder rather than a query, in the shape `app.follow_queries` established and for
    the same reason: the batch half hands it to a statement built in `pipeline_run`, where there
    is no session to hand it. `correlate(None)` keeps the subquery standalone whatever FROM list
    it lands in."""
    branches: list[ColumnElement[bool]] = []
    scoped_id = None if only is None else only[1]

    def wants(entity_type: str) -> bool:
        return only is None or only[0] == entity_type

    if wants("person"):
        branches.append(
            Film.id.in_(
                select(FilmCredit.film_id).where(
                    watchlist_credit_clause(),
                    FilmCredit.person_id.in_(
                        followed_tmdb_ids(user_id, "person", entity_id=scoped_id)
                    ),
                )
            )
        )
    if wants("company"):
        branches.append(
            Film.id.in_(
                select(FilmProductionCompany.film_id).where(
                    FilmProductionCompany.company_id.in_(
                        followed_tmdb_ids(user_id, "company", entity_id=scoped_id)
                    )
                )
            )
        )
    if wants("franchise"):
        branches.append(
            Film.collection_id.in_(followed_tmdb_ids(user_id, "franchise", entity_id=scoped_id))
        )
    if wants("title"):
        branches.append(Film.id.in_(followed_film_uuids(user_id, entity_id=scoped_id)))

    already_listed = exists().where(
        WatchlistItem.user_id == user_id, WatchlistItem.film_id == Film.id
    )
    dismissed = exists().where(
        WatchlistDismissal.user_id == user_id, WatchlistDismissal.film_id == Film.id
    )
    return (
        select(Film.id)
        .where(
            in_play_clause(today=today, excluded_statuses=excluded_statuses),
            or_(*branches),
            ~already_listed,
            ~dismissed,
        )
        .correlate(None)
    )


async def derive_for_user(
    session: AsyncSession,
    *,
    user_id: UUID,
    today: date,
    excluded_statuses: frozenset[str],
    only: tuple[str, str] | None = None,
) -> int:
    """Add this user's missing derived items and return how many rows were written. No commit.

    `alert_prefs` is left to the column's server default (`{stream}`, D-14) rather than restated
    here, so the default lives in one place for every writer of these rows."""
    scope = derivable_film_ids(
        user_id=user_id, today=today, excluded_statuses=excluded_statuses, only=only
    ).subquery("derivable")
    rows = select(
        literal(user_id, type_=PGUUID(as_uuid=True)).label("user_id"),
        scope.c.id.label("film_id"),
        literal(DERIVED_SOURCE).label("source"),
    )
    # `RETURNING` rather than the cursor's row count: with `ON CONFLICT DO NOTHING` it names
    # the rows this statement actually inserted, which is what a caller reporting "items
    # derived" means — a conflict is a row somebody else wrote, not one of ours.
    inserted = await session.execute(
        pg_insert(WatchlistItem)
        .from_select(["user_id", "film_id", "source"], rows)
        .on_conflict_do_nothing(index_elements=["user_id", "film_id"])
        .returning(WatchlistItem.film_id)
    )
    return len(inserted.scalars().all())


async def derive_for_follow(
    session: AsyncSession, *, user_id: UUID, entity_type: str, entity_id: str
) -> int:
    """Derive from one just-created follow (D-13's synchronous half) and return how many items
    were added. No commit — the follow and its items are one transaction.

    Resolves its own clock and statuses, like `public.service.get_timeline`: this is the
    request-time entry point, and the alternative is every route and import that creates a
    follow assembling the same two arguments. The batch half is passed them, because the sweep
    fixes one `today` for the whole run."""
    return await derive_for_user(
        session,
        user_id=user_id,
        today=datetime.now(UTC).date(),
        excluded_statuses=get_settings().tmdb_excluded_statuses,
        only=(entity_type, entity_id),
    )
