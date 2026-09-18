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

**D-11's second half is a second builder, not a fifth branch (NEU-1365).** Events that *name* a
resolved followed person on a film they hold no credit on select **events**, not films: a person
named in a story about a film they are not credited on makes *that event* timeline-worthy, not
every event the film has ever had. So it lives in `events_naming_followed_people` below, an
event-level predicate the timeline OR-s against this filter rather than into this OR, and
`get_feed_grouped` carries it in its own parameter beside the film one. Widening `followed_film_ids`
to cover it would silently pull in the film's whole history.
"""

from datetime import date
from uuid import UUID

from sqlalchemy import Integer, Select, and_, cast, or_, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from upmovies.app.models import Follow
from upmovies.catalog.models import Film, FilmCredit, FilmProductionCompany
from upmovies.catalog.queries import in_play_clause, seed_grade_credit_clause
from upmovies.news.models import RESOLVED_MENTION_PATHS, EventStory, StoryPerson


def followed_tmdb_ids(
    user_id: UUID, entity_type: str, *, entity_id: str | None = None
) -> Select[tuple[int]]:
    """The TMDB ids this user follows under `entity_type`, or just `entity_id` when one is named.

    Public because the derived-watchlist pass (D-13, `app.services.derivation_service`) reads the
    follow graph with a different *film* rule but the same *id* rule, and a second spelling of the
    cast and the guard below is how the two would drift apart. `entity_id` narrows the graph to one
    row for the follow-creation half of that pass, which derives from the follow just made rather
    than re-deriving everything the user follows.

    `entity_id` is polymorphic text, so person, company and franchise ids have to be cast back
    to integers to meet the catalog's keys. The digit guard sits beside the `entity_type` filter
    in the same SELECT, so the cast only ever sees a row of this type whose value is a decimal
    integer, and a row that is neither is skipped rather than failing the statement.

    Belt and braces on top of `app.dto.normalise_entity_id`, which is what *should* keep a
    non-numeric id out of the table — but it is a boundary rule, applied by the follow routes'
    request models, while `follow_service.follow` takes an `entity_id` straight from its caller
    and the imports (D-15, D-16) are about to become such callers. A row that slipped through
    would otherwise not degrade this filter, it would abort the whole statement: every branch is
    OR-ed into one subquery, so the user's entire timeline would 500 — and the notify pass
    (NEU-1379), which runs this over every user at once, would fail for all of them over one
    bad row."""
    stmt = select(cast(Follow.entity_id, Integer)).where(
        Follow.user_id == user_id,
        Follow.entity_type == entity_type,
        Follow.entity_id.regexp_match(r"^[0-9]+$"),
    )
    return stmt if entity_id is None else stmt.where(Follow.entity_id == entity_id)


def followed_film_uuids(user_id: UUID, *, entity_id: str | None = None) -> Select[tuple[UUID]]:
    """The film ids this user follows by title — `entity_id` is our UUID for that type — or just
    `entity_id` when one is named, on the same terms as `followed_tmdb_ids`.

    Shape-guarded for the same reason and at the same cost as the digit guard above: `title` is
    the one type whose id is not an integer, so it needs its own pattern, and a row that is not
    a UUID would abort the statement rather than degrade it. The batch callers are what make
    that expensive — the derivation pass (D-13) would raise for that user on every sweep for as
    long as the row exists, and the notify pass (NEU-1379) for every user at once."""
    stmt = select(cast(Follow.entity_id, PGUUID(as_uuid=True))).where(
        Follow.user_id == user_id,
        Follow.entity_type == "title",
        Follow.entity_id.regexp_match(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"),
    )
    return stmt if entity_id is None else stmt.where(Follow.entity_id == entity_id)


def followed_film_ids(
    *, user_id: UUID, today: date, excluded_statuses: frozenset[str]
) -> Select[tuple[UUID]]:
    """`SELECT film.id` for every film this user's follows reach (D-11).

    Written to be used as `Film.id.in_(followed_film_ids(...))`. `correlate(None)` keeps the
    subquery standalone whatever the caller's FROM list holds: the timeline's enclosing query
    selects from `catalog.film` too, and SQLAlchemy would otherwise auto-correlate this
    subquery's own `film` to the outer one and render it without a FROM. Inside an `IN` over
    `film.id` that happens to come out to the same rows, so this is not load-bearing there — it
    is what stops the builder's meaning depending on the query it is dropped into, which is the
    whole premise of handing the same SELECT to the notify pass (NEU-1379).

    A user with no follows yields no rows, so the timeline of an empty follow graph is empty
    rather than the whole feed — the onboarding prompt D-12 asks for is the client's call to
    make off `total == 0`, not something this hides by falling back.
    """
    person_films = select(FilmCredit.film_id).where(
        seed_grade_credit_clause(),
        FilmCredit.person_id.in_(followed_tmdb_ids(user_id, "person")),
    )
    company_films = select(FilmProductionCompany.film_id).where(
        FilmProductionCompany.company_id.in_(followed_tmdb_ids(user_id, "company"))
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
                Film.collection_id.in_(followed_tmdb_ids(user_id, "franchise")),
                Film.id.in_(followed_film_uuids(user_id)),
            )
        )
        .correlate(None)
    )


def events_naming_followed_people(user_id: UUID) -> Select[tuple[UUID]]:
    """`SELECT event.id` for every event whose stories name a *resolved* person this user follows
    — D-11's second half (NEU-1365).

    Written to be used as `Event.id.in_(events_naming_followed_people(user_id))`, OR-ed against
    `followed_film_ids` rather than folded into it, for the reason the module docstring gives:
    this narrows *events*, and the film it happens to hang off has no other claim on the timeline.

    **`RESOLVED_MENTION_PATHS` is the cut: `accepted` and `tiebreak` match, `unlinked` and
    `not_in_tmdb` never do (D-25).** A tiebreak
    the resolve stage decided keeps its route precisely so a human can find it again (D-22), and
    it names a person, so it belongs on the timeline as much as an accept does. One nobody was
    named in carries `person_id` NULL and drops out of the `IN` on its own — which is why the
    path filter needs no null guard beside it, and why the two cannot be collapsed into "has a
    `person_id`": an `unlinked` row written with a candidate id would then match.

    **No in-play term**, unlike the credit branch. That cut exists because a credit reaches a
    whole filmography and the back catalogue would flood the timeline; a mention reaches exactly
    one event, published now, so there is nothing to flood with — and an event about a released
    film that names a followed person is news about them either way. Event and film visibility
    stay the feed's, applied by the query this is dropped into.

    `correlate(None)` for the reason `followed_film_ids` gives: the timeline's enclosing query
    reaches `news.event_story` through the news-backed EXISTS, and this subquery's meaning must
    not depend on the query it is dropped into.
    """
    resolved_mentions = select(StoryPerson.story_id).where(
        StoryPerson.path.in_(RESOLVED_MENTION_PATHS),
        StoryPerson.person_id.in_(followed_tmdb_ids(user_id, "person")),
    )
    return (
        select(EventStory.event_id)
        .where(EventStory.story_id.in_(resolved_mentions))
        .correlate(None)
    )
