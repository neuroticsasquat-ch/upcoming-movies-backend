"""Shared query predicates over `catalog.film`."""

from collections.abc import Collection
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmCredit, FilmFieldChange
from upmovies.catalog.seed_grade import (
    DIRECTOR_JOB,
    TOP_BILLED_ORDER,
    WRITER_JOBS,
    is_seed_grade,
    recorded_credit_key,
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


ALERT_WINDOW_DEAD_STATUSES: frozenset[str] = frozenset({"Canceled"})
"""The one TMDB status past which no follow is owed anything about a film (D-46).

`Released` is deliberately **not** here: it is the state the home-release beats happen in —
`now_available` (D-28), the `US:digital` / `US:physical` release dates (D-26), the late trailer
(D-35) — so a window that ended there delivered none of them to an indirect follower.
"""


def alert_window_clause(*, today: date, max_age_days: int) -> ColumnElement[bool]:
    """WHERE predicate selecting films a follow still covers *for alerts* (D-43, D-46) — released
    up to `max_age_days` ago, undated, or still to come, and not called off.

    `in_play_clause` with its release bound moved back by `max_age_days` instead of cutting at
    `today`, and with a status term of its own, and both differences are the point. A film that
    opened last month has not finished happening: its `now_available` beat fires 14 to 365 days
    after the theatrical date, and an in-play cut would have dropped the film from every follow
    that covers it the morning it came out — which is exactly when the user is waiting to hear
    that they can watch it.

    **Why the status term is not in-play's (D-46, NEU-1417).** `TMDB_EXCLUDED_STATUSES` holds
    `Released,Canceled`, and reusing it here took back what the moved date bound gave: a film
    left an indirect follow's coverage the day TMDB marked it `Released`, which is precisely the
    state the beats this window exists to deliver land in. The window's own term is
    `ALERT_WINDOW_DEAD_STATUSES` — `Canceled` alone, the one state with nothing left to deliver.
    It is a module constant rather than a second setting because TMDB's status vocabulary is
    closed (`Rumored`, `Planned`, `In Production`, `Post Production`, `Released`, `Canceled`) and
    only one member of it is dead, so there is nothing to tune from Coolify; with ten call sites,
    a parameter's live risk was somebody handing an alert-window builder the in-play set by
    mistake, and a constant makes that unspellable. `TMDB_EXCLUDED_STATUSES` still governs
    admission, `in_play_clause`, `active_film_clause`, the sweep and the D-11 timeline builder.

    `max_age_days` is `PROVIDER_POLL_MAX_AGE_DAYS` at every call site, deliberately rather than
    a number of its own: the provider poll stops looking for offers on a film that old, so past
    it there is nothing left for the coverage to deliver, and one constant keeps the two from
    disagreeing about when a film is finished. Tuning it is a provider-poll decision that moves
    both.

    Why any bound at all: admission already keeps the back catalogue out of the catalog — a
    `Released` film is skipped at ingest (`ingest.tmdb.filters.classify_skip`), so a film is only
    here because it was admitted before release and aged in place, and a company follow cannot
    reach twenty years of output. The date bound is therefore a ceiling on how long a followed
    film keeps costing a poll a day and keeps a place on the watchlist, not a defence against a
    flood that is already in the catalog.

    The NULL guards are `in_play_clause`'s, for the same reason: `NULL NOT IN (...)` is NULL,
    which would drop undated films and films of unknown status rather than keep them.
    """
    return and_(
        or_(Film.release_date.is_(None), Film.release_date >= today - timedelta(days=max_age_days)),
        or_(Film.status.is_(None), Film.status.not_in(ALERT_WINDOW_DEAD_STATUSES)),
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


async def present_recorded_credits(
    session: AsyncSession, *, film_ids: set[UUID], followed: Collection[int] = ()
) -> dict[tuple[UUID, int, str, str | None], int | None]:
    """Every `(film, person, recorded role, job)` that `catalog.film_credit` holds *right now*
    for these films, mapped to that credit's billing order.

    The key is `catalog.seed_grade.role_match_key`, so a `crew` credit is matched on its job
    and every other role is matched on the role alone — a reverted `Gaffer` credit must not
    read as still present because the person also holds `Best Boy`.

    One query for a whole set of films rather than one per credit: every caller asks it of a
    batch — the sweep's quarantine gate of an aged backlog, its burst check of the same rows,
    the Tier-A short-circuit of one film's pending changes — and the rolling window makes that
    the same rows on every pass for as long as a hold lasts.

    **Recorded grade, not seed grade** (D-49): a credit is here when it is seed grade *or* its
    person is in `followed` — the people somebody follows at coverage `any`
    (`app.follow_queries.people_followed_at_any`). That is the same rule `credit_history`
    applied when it wrote the change row, which is what makes this answer the question the
    callers actually ask: "is the credit this row recorded still there, under the same role?"
    A default of no followed people keeps every caller that only ever asks about seed grade —
    and every test written before M9 — reading exactly what it used to.

    The grade is re-derived here rather than assumed from the `film_credit_change` row that
    recorded the attachment. `film_credit` is delete-and-rebuilt on every ingest, and a cast
    member who has since slipped out of the top-5 billing no longer holds a seed-grade credit
    — which is exactly how `credit_history` would diff them, as removed. Reading the same
    predicate is what stops the callers disagreeing about what "still attached" means, which
    is also why it lives here beside `seed_grade_credit_clause` rather than in any one of them.

    **`followed` is read live, and shared with the writer.** D-49 says the gate checks presence
    rather than the follow, and this does — it asks nothing about *who* is looking, and a
    credit's presence is a property of the film. What it does not do is remember the follow set
    a change was recorded under: passing a stale one is how this and `credit_history` would
    come to mean two different things by "recorded", and the next ingest would then write a
    removal for a credit this had just published an attachment for. The cost is the narrow
    case where a follow is narrowed while its credit is still in quarantine: the pending
    attachment is held from then on and ages out uncarded, which is the same end state as the
    reverted edit beside it and the coherent one — the credit has stopped being recorded, so
    there is nothing left to announce.

    Membership answers "is this credit still there"; the value answers ADR-0017 D-7's body
    ordering. Both are properties of the same live row, so they are read together —
    `film_credit_change` records no billing position of its own, and asking for it in a second
    query would be asking twice.
    """
    if not film_ids:
        return {}
    watched = set(followed)
    stmt = select(
        FilmCredit.film_id,
        FilmCredit.person_id,
        FilmCredit.credit_type,
        FilmCredit.job,
        FilmCredit.credit_order,
    ).where(FilmCredit.film_id.in_(film_ids))
    present: dict[tuple[UUID, int, str, str | None], int | None] = {}
    for row in await session.execute(stmt):
        if not (
            is_seed_grade(row.credit_type, row.job, row.credit_order) or row.person_id in watched
        ):
            continue
        present[(row.film_id, row.person_id, *recorded_credit_key(row.credit_type, row.job))] = (
            row.credit_order
        )
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
