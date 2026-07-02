"""Shared query predicates over `catalog.film`."""

from datetime import date

from sqlalchemy import ColumnElement, and_, or_

from upmovies.catalog.models import Film


def active_film_clause(*, today: date, excluded_statuses: frozenset[str]) -> ColumnElement[bool]:
    """WHERE predicate selecting films still in play (not released/canceled).

    A film is INACTIVE when ``release_date < today`` OR ``status`` is in
    ``excluded_statuses``; this returns the negation. The NULL guards keep undated
    films and films with an unknown status in the active set — without them SQL's
    ``NULL NOT IN (...)`` evaluates to NULL and would wrongly drop those rows.
    """
    return and_(
        or_(Film.release_date.is_(None), Film.release_date >= today),
        or_(Film.status.is_(None), Film.status.not_in(excluded_statuses)),
    )
