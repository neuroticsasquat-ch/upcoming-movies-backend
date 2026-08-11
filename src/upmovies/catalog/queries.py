"""Shared query predicates over `catalog.film`."""

from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmFieldChange

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
