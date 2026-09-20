"""Shared query predicates over `catalog.film`."""

from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmCredit, FilmFieldChange
from upmovies.catalog.seed_grade import (
    DIRECTOR_JOB,
    TOP_BILLED_ORDER,
    WRITER_JOBS,
    credit_role,
    is_seed_grade,
)

# The one place `catalog` reads `news`. Dormancy is defined partly by what news did (or did
# not) link, so the predicate cannot be expressed without `Story`; `news.models` imports
# nothing but `upmovies.db`, so the coupling `news.fetcher` already has in the other
# direction stays acyclic.
from upmovies.news.models import Story


def active_film_clause(
    *, today: date, excluded_statuses: frozenset[str], dormancy_days: int
) -> ColumnElement[bool]:
    """WHERE predicate selecting films still in play (not released/canceled/dormant).

    A film is INACTIVE when ``release_date < today`` OR ``status`` is in
    ``excluded_statuses``; this returns the negation. The NULL guards keep undated
    films and films with an unknown status in the active set — without them SQL's
    ``NULL NOT IN (...)`` evaluates to NULL and would wrongly drop those rows.

    **Dormancy** (ADR-0015) closes the hole that leaves: a dated film ages out by
    itself, but ``release_date IS NULL`` is permanently active and TMDB rarely marks
    a dead project ``Canceled``, so without a rule the working set grows forever. An
    undated film is dormant when, for ``dormancy_days``, TMDB recorded no semantic
    change to it (no ``catalog.film_field_change`` row — the trigger's denylist already
    strips popularity and vote churn) *and* no story linked to it. The window is
    measured from ``film.created_at`` when neither signal exists, so a newly admitted
    film is not born dormant.

    Dormancy is **derived, never stored**: any later change or linked story revives the
    film on the next read, with no intervention. It is also keyed on quiescence rather
    than age — a film can be real and quiet for a year. And it only ever narrows the
    active set; a busy film that is released or canceled stays out.

    Callers pass ``today`` so a past date reconstructs the active set as it stood then.
    That replay is approximate for dormancy, in both directions: the signal subqueries are
    unbounded, so a change recorded *after* the replay date still counts, while a film
    admitted long before it and quiet since drops out. Neither bites the dated fixtures the
    replay serves — dormancy never applies to a dated film — and production passes no date.
    """
    return and_(
        in_play_clause(today=today, excluded_statuses=excluded_statuses),
        not_(dormant_film_clause(today=today, dormancy_days=dormancy_days)),
    )


def in_play_clause(*, today: date, excluded_statuses: frozenset[str]) -> ColumnElement[bool]:
    """WHERE predicate selecting films that have neither released nor been called off —
    ``active_film_clause`` without its dormancy term.

    Split out for the sweep's refresh phase, which covers dormant films too and so cannot
    use the composed predicate (§4.5). Nothing else should reach for it: dormancy is part
    of what "active" means everywhere the working set is being *spent* on.

    The NULL guards keep undated films and films with an unknown status in the set —
    without them SQL's ``NULL NOT IN (...)`` evaluates to NULL and would wrongly drop
    those rows.
    """
    return and_(
        or_(Film.release_date.is_(None), Film.release_date >= today),
        or_(Film.status.is_(None), Film.status.not_in(excluded_statuses)),
    )


def alert_window_clause(
    *, today: date, excluded_statuses: frozenset[str], max_age_days: int
) -> ColumnElement[bool]:
    """WHERE predicate selecting films a follow still covers *for alerts* (D-43) — released up
    to `max_age_days` ago, undated, or still to come, and not called off.

    `in_play_clause` with its release bound moved back by `max_age_days` instead of cutting at
    `today`, and the difference is the whole point. A film that opened last month has not
    finished happening: its `now_available` beat fires 14 to 200 days after the theatrical date,
    and an in-play cut would have dropped the film from every follow that covers it the morning
    it came out — which is exactly when the user is waiting to hear that they can watch it.

    `max_age_days` is `PROVIDER_POLL_MAX_AGE_DAYS` at every call site, deliberately rather than
    a number of its own: the provider poll stops looking for offers on a film that old, so past
    it there is nothing left for the coverage to deliver, and one constant keeps the two from
    disagreeing about when a film is finished. Tuning it is a provider-poll decision that moves
    both.

    Why any bound at all: a company or franchise follow otherwise covers its entire back
    catalogue, and through `follow_queries.covered_by_any_user_clause` the provider poll would
    then read every film in it, every day.

    The NULL guards are `in_play_clause`'s, for the same reason: `NULL NOT IN (...)` is NULL,
    which would drop undated films and films of unknown status rather than keep them.

    **Known limit, spelled out because it bounds what the relaxed date bound buys
    (NEU-1414).** `excluded_statuses` defaults to `Released,Canceled`, so a film TMDB has
    marked `Released` is outside this window whatever its date — which is most of the
    population the moved date bound was aimed at. What the relaxation still reaches is the
    film whose status TMDB has not caught up on and the one that carries none at all, and a
    **title** follow is unaffected either way (it covers its film in any state, which is what
    keeps the case the user asked for whole). Narrowing the status term to cancellation alone
    would widen this to every recently-released film, and that is a product decision about
    what a person follow is worth after release, not a detail to change under a rename: it
    belongs with the `PROVIDER_POLL_MAX_AGE_DAYS` tuning ticket, which already owns this
    window's width.
    """
    return and_(
        or_(Film.release_date.is_(None), Film.release_date >= today - timedelta(days=max_age_days)),
        or_(Film.status.is_(None), Film.status.not_in(excluded_statuses)),
    )


def seed_grade_credit_clause() -> ColumnElement[bool]:
    """WHERE predicate over `catalog.film_credit` selecting the seed-grade credits — director,
    Writer/Screenplay, or top-5 billed cast (`catalog.seed_grade`).

    The SQL spelling of `seed_grade.is_seed_grade`, which decides the same cut one loaded row at
    a time. It lives here rather than in `seed_grade` itself because that module is deliberately
    free of SQLAlchemy — it is read by the TMDB payload side too — but it must exist exactly
    once: the sweep's seed set and the timeline's person follows (D-11) both ask it of stored
    rows, and a second spelling of the expression is precisely how the definition drifts, which
    `catalog.seed_grade` exists to prevent.

    A predicate over `film_credit` alone: it says nothing about the film, so a caller that cares
    whether the film is still in play composes `in_play_clause` beside it.
    """
    return or_(
        and_(FilmCredit.credit_type == "crew", FilmCredit.job == DIRECTOR_JOB),
        and_(FilmCredit.credit_type == "crew", FilmCredit.job.in_(WRITER_JOBS)),
        and_(
            FilmCredit.credit_type == "cast",
            FilmCredit.credit_order.is_not(None),
            FilmCredit.credit_order < TOP_BILLED_ORDER,
        ),
    )


async def present_seed_credits(
    session: AsyncSession, *, film_ids: set[UUID]
) -> dict[tuple[UUID, int, str], int | None]:
    """Every `(film, person, seed-grade role)` that `catalog.film_credit` holds *right now*
    for these films, mapped to that credit's billing order.

    One query for a whole set of films rather than one per credit: every caller asks it of a
    batch — the sweep's quarantine gate of an aged backlog, its burst check of the same rows,
    the Tier-A short-circuit of one film's pending changes — and the rolling window makes that
    the same rows on every pass for as long as a hold lasts.

    Seed grade is re-derived here rather than assumed from the `film_credit_change` row that
    recorded the attachment. `film_credit` is delete-and-rebuilt on every ingest, and a cast
    member who has since slipped out of the top-5 billing no longer holds a seed-grade credit
    — which is exactly how `credit_history` would diff them, as removed. Reading the same
    predicate is what stops the callers disagreeing about what "still attached" means, which
    is also why it lives here beside `seed_grade_credit_clause` rather than in any one of them.

    Membership answers "is this credit still there"; the value answers ADR-0017 D-7's body
    ordering. Both are properties of the same live row, so they are read together —
    `film_credit_change` records no billing position of its own, and asking for it in a second
    query would be asking twice.
    """
    if not film_ids:
        return {}
    stmt = select(
        FilmCredit.film_id,
        FilmCredit.person_id,
        FilmCredit.credit_type,
        FilmCredit.job,
        FilmCredit.credit_order,
    ).where(FilmCredit.film_id.in_(film_ids))
    present: dict[tuple[UUID, int, str], int | None] = {}
    for row in await session.execute(stmt):
        if not is_seed_grade(row.credit_type, row.job, row.credit_order):
            continue
        role = credit_role(row.credit_type, row.job)
        if role is not None:
            present[(row.film_id, row.person_id, role)] = row.credit_order
    return present


def dormant_film_clause(*, today: date, dormancy_days: int) -> ColumnElement[bool]:
    """WHERE predicate selecting films that have gone dormant (ADR-0015).

    A film is dormant when it is **undated** and, for ``dormancy_days``, TMDB recorded no
    semantic change to it (no ``catalog.film_field_change`` row) *and* no story linked to
    it. Dated films are never dormant however quiet they are — they age out by release
    date instead. The window is measured from ``film.created_at`` when neither signal
    exists, so a newly admitted film is not born dormant.

    Says nothing about release or cancellation: this is one half of
    ``active_film_clause``, not a standalone answer to "is this film worth anything".
    """
    quiescent_before = today - timedelta(days=dormancy_days)
    last_change = (
        select(func.max(FilmFieldChange.changed_at))
        .where(FilmFieldChange.film_id == Film.id)
        .scalar_subquery()
    )
    # `linked_at` is set alongside `film_id` by every linking path; the coalesce is a
    # belt-and-braces guard so a row that somehow lacks it still counts as a signal
    # rather than silently reading as "never linked".
    last_link = (
        select(func.max(func.coalesce(Story.linked_at, Story.created_at)))
        .where(Story.film_id == Film.id)
        .scalar_subquery()
    )
    # GREATEST ignores NULL inputs, and `created_at` is NOT NULL, so this is never NULL.
    last_signal = func.greatest(Film.created_at, last_change, last_link)
    return and_(Film.release_date.is_(None), last_signal < quiescent_before)


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
