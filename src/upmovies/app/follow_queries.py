"""The follow graph read as a *filter* — which films a user's follows reach (D-11).

Beside `app/entitlements.py` and for the same reason: the callers are not all here yet. The
timeline (`public.service.get_timeline`) asks this today; the notify pass (NEU-1379) asks the
same question of every user at once, in SQL, because a batch pass cannot lean on a route
dependency. So this is a **query builder, not a query**: it takes a `user_id` and returns a
`SELECT film.id`, holds no session, and reads nothing request-scoped — that is what makes it
callable from `pipeline_run` as well as from a route.

The four branches are D-11's, one per `follow.entity_type`:

- **person** — films the followed person holds a *seed-grade* credit on (`catalog.seed_grade`:
  director, Writer/Screenplay, top-5 billed) **and** that are still in play. The in-play term is
  this branch's alone: following a person is a standing interest in what they are making next,
  and their back catalogue would otherwise flood the timeline. Following a *title*, a company or
  a franchise is a request for that specific thing, released or not.
- **company** — `catalog.film_production_company`.
- **franchise** — `film.collection_id`.
- **title** — the film itself.

**Seam for M4 (NEU-1365).** D-11's second half — events that *name* a resolved followed person
on a film they hold no credit on — does not belong in this function, and not because it is
unwritten: it selects **events**, not films. A person named in a story about a film they are not
credited on makes *that event* timeline-worthy, not every event the film has ever had. NEU-1365
therefore adds `events_naming_followed_people(user_id)` beside this — an event-level predicate,
OR-ed into the timeline's event scope rather than into this OR — and `get_feed_grouped` grows
the event-filter parameter to carry it, in the same shape as the film filter it already takes.
Widening this function to cover it instead would silently pull in the film's whole history.
"""

from datetime import date
from uuid import UUID

from sqlalchemy import Integer, Select, and_, cast, or_, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from upmovies.app.models import Follow
from upmovies.catalog.models import Film, FilmCredit, FilmProductionCompany
from upmovies.catalog.queries import in_play_clause, seed_grade_credit_clause


def _followed_tmdb_ids(user_id: UUID, entity_type: str) -> Select[tuple[int]]:
    """The TMDB ids this user follows under `entity_type`.

    The cast is safe because `entity_id` is polymorphic text normalised on the way in
    (`app.dto.normalise_entity_id`): a person, company or franchise follow can only hold the
    decimal spelling of a positive integer. It sits in the same SELECT as the `entity_type`
    filter, so no row of another type is ever cast."""
    return select(cast(Follow.entity_id, Integer)).where(
        Follow.user_id == user_id, Follow.entity_type == entity_type
    )


def _followed_film_uuids(user_id: UUID) -> Select[tuple[UUID]]:
    """The film ids this user follows by title — `entity_id` is our UUID for that type."""
    return select(cast(Follow.entity_id, PGUUID(as_uuid=True))).where(
        Follow.user_id == user_id, Follow.entity_type == "title"
    )


def followed_film_ids(
    *, user_id: UUID, today: date, excluded_statuses: frozenset[str]
) -> Select[tuple[UUID]]:
    """`SELECT film.id` for every film this user's follows reach (D-11).

    Written to be used as `Film.id.in_(followed_film_ids(...))`. `correlate(None)` is
    load-bearing: the enclosing query selects from `catalog.film` too, and SQLAlchemy would
    otherwise auto-correlate this subquery's own `film` to the outer one — turning "the films
    you follow" into "this film, if you follow it" by way of a FROM-less subquery. Spelling it
    out keeps the subquery standalone whatever the caller's FROM list holds.

    A user with no follows yields no rows, so the timeline of an empty follow graph is empty
    rather than the whole feed — the onboarding prompt D-12 asks for is the client's call to
    make off `total == 0`, not something this hides by falling back.
    """
    person_films = select(FilmCredit.film_id).where(
        seed_grade_credit_clause(),
        FilmCredit.person_id.in_(_followed_tmdb_ids(user_id, "person")),
    )
    company_films = select(FilmProductionCompany.film_id).where(
        FilmProductionCompany.company_id.in_(_followed_tmdb_ids(user_id, "company"))
    )
    return (
        select(Film.id)
        .where(
            or_(
                and_(
                    Film.id.in_(person_films),
                    in_play_clause(today=today, excluded_statuses=excluded_statuses),
                ),
                Film.id.in_(company_films),
                Film.collection_id.in_(_followed_tmdb_ids(user_id, "franchise")),
                Film.id.in_(_followed_film_uuids(user_id)),
            )
        )
        .correlate(None)
    )
