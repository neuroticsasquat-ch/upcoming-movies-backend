"""The follow graph read as a *filter* — which films a user's follows reach.

Beside `app/entitlements.py` and for the same reason: the callers are not all here yet. The
timeline (`public.service.get_timeline`) asks this today; the notify pass (NEU-1379) asks the
same question of every user at once, in SQL, because a batch pass cannot lean on a route
dependency. So these are **query builders, not queries**: each takes a `user_id` and returns a
`Select` (or a WHERE predicate), holds no session, and reads nothing request-scoped — that is
what makes them callable from `pipeline_run` as well as from a route.

**Two questions, two sets (M8, ADR-0018).** A follow is now the only thing a user keeps, and it
answers both of them:

- *What belongs on my timeline?* — `followed_film_ids` and `events_naming_followed_people`,
  D-11's two halves. Every follow reaches every seed-grade credit here, whatever its coverage.
- *What am I waiting to hear about?* — `covered_film_ids`, and `watchlist_film_ids` once the
  mutes are taken out. **That set is the watchlist**: there is no `app.watchlist_item` any
  more, so `/me/calendar`, the iCal feed, the notify pass's alert branch and the digest's
  slate all read this and nothing else.

The two differ in three deliberate ways, and only these three: a person follow's coverage
(D-43) narrows which credits alert, the alert window (D-1414.2, D-46) bounds how long a follow
keeps covering a film and in which statuses, and a **mute** (`app.watchlist_dismissal`)
subtracts films from both — since D-45 a mute silences the film everywhere, so the exclusion
lives *inside* the D-11 builders rather than at their call sites.

The four branches are one per `follow.entity_type`:

- **person** — films the followed person holds a credit on: *seed-grade* for the timeline
  (`catalog.seed_grade`: director, Writer/Screenplay, top-5 billed), the coverage's cut for
  alerts. The in-play/alert-window term is this branch's alone on the timeline side: following
  a person is a standing interest in what they are making next, and their back catalogue would
  otherwise flood it. Following a *title*, a company or a franchise is a request for that
  specific thing, released or not.
- **company** — `catalog.film_production_company`.
- **franchise** — `film.collection_id`.
- **title** — the film itself, in any state. The user asked for that film.

**D-11's second half is a second builder, not a fifth branch (NEU-1365).** Events that *name* a
resolved followed person on a film they hold no credit on select **events**, not films: a person
named in a story about a film they are not credited on makes *that event* timeline-worthy, not
every event the film has ever had. So it lives in `events_naming_followed_people` below, an
event-level predicate the timeline OR-s against this filter rather than into this OR, and
`get_feed_grouped` carries it in its own parameter beside the film one. Widening `followed_film_ids`
to cover it would silently pull in the film's whole history.

**Every id cast is spelled in a SELECT list, never in a WHERE.** `entity_id` is polymorphic
text, so the three integer types have to be cast back to meet the catalog's keys — and Postgres
does not promise to evaluate `entity_type = 'person'` before the cast beside it. A row of the
wrong shape would then not degrade a filter, it would abort the statement: the user's whole
timeline would 500, and the batch passes would fail for every user over one bad row. Projecting
the cast means the WHERE (the type filter and the shape guard) has already run when it happens.
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Integer, Select, and_, cast, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from upmovies.app.models import Follow, WatchlistDismissal
from upmovies.catalog.models import Film, FilmCredit, FilmProductionCompany
from upmovies.catalog.queries import (
    alert_window_clause,
    in_play_clause,
    seed_grade_credit_clause,
)
from upmovies.catalog.seed_grade import DIRECTOR_JOB
from upmovies.news.models import RESOLVED_MENTION_PATHS, Event, EventStory, StoryPerson

_INT_ID_PATTERN = r"^[0-9]+$"
_UUID_PATTERN = r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"

LEAD_TOP_BILLED_ORDER = 3
"""`coverage = 'lead'`'s billing cut, on TMDB's 0-indexed `order`: slots 0, 1 and 2.

Not `catalog.seed_grade.TOP_BILLED_ORDER`, which is 5, and not a candidate for being unified
with it. The two answer different questions and the numbers are load-bearing in opposite
directions: seed grade decides whose filmography the sweep *enumerates*, where the measurement
at NEU-1090 shows cutting to 3 discards demonstrably-shooting films at 14:1, while this decides
whose casting is worth a **push**, where 5 would notify a user about a supporting role they did
not follow the person for. `coverage = 'all'` is how a user who wants the wider cut asks for it
(D-43), which is the affordance that makes the narrow default safe."""


def followed_tmdb_ids(
    user_id: UUID, entity_type: str, *, entity_id: str | None = None
) -> Select[tuple[int]]:
    """The TMDB ids this user follows under `entity_type`, or just `entity_id` when one is named.

    `entity_id` narrows the graph to one row for the callers that ask about a single follow —
    the want/stop service asking "does anything *else* cover this film" — so they cost one
    branch's work instead of four.

    The digit guard sits beside the `entity_type` filter in the same SELECT, so the projected
    cast only ever sees a row of this type whose value is a decimal integer, and a row that is
    neither is skipped rather than failing the statement (see the module docstring).

    Belt and braces on top of `app.dto.normalise_entity_id`, which is what *should* keep a
    non-numeric id out of the table — but it is a boundary rule, applied by the follow routes'
    request models, while `follow_service.follow` takes an `entity_id` straight from its caller
    and the imports (D-15, D-16) are such callers."""
    stmt = select(cast(Follow.entity_id, Integer)).where(
        Follow.user_id == user_id,
        Follow.entity_type == entity_type,
        Follow.entity_id.regexp_match(_INT_ID_PATTERN),
    )
    return stmt if entity_id is None else stmt.where(Follow.entity_id == entity_id)


def followed_film_uuids(user_id: UUID, *, entity_id: str | None = None) -> Select[tuple[UUID]]:
    """The film ids this user follows by title — `entity_id` is our UUID for that type — or just
    `entity_id` when one is named, on the same terms as `followed_tmdb_ids`.

    Shape-guarded for the same reason and at the same cost as the digit guard above: `title` is
    the one type whose id is not an integer, so it needs its own pattern, and a row that is not
    a UUID would abort the statement rather than degrade it. The batch callers are what make
    that expensive — the notify pass (NEU-1379) would raise for every user at once."""
    stmt = select(cast(Follow.entity_id, PGUUID(as_uuid=True))).where(
        Follow.user_id == user_id,
        Follow.entity_type == "title",
        Follow.entity_id.regexp_match(_UUID_PATTERN),
    )
    return stmt if entity_id is None else stmt.where(Follow.entity_id == entity_id)


def muted_film_ids(user_id: UUID) -> Select[tuple[UUID]]:
    """`SELECT film_id` for every film this user has muted (D-45).

    The one set that *subtracts*. Written to be used as `Film.id.not_in(muted_film_ids(u))` —
    or `Event.film_id.not_in(...)`, which is safe because `news.event.film_id` is NOT NULL and
    a `NOT IN` over a nullable column would drop every row instead."""
    return select(WatchlistDismissal.film_id).where(WatchlistDismissal.user_id == user_id)


def lead_credit_clause() -> ColumnElement[bool]:
    """WHERE predicate over `catalog.film_credit` selecting `coverage = 'lead'`'s credits: a
    director credit, or a cast credit billed in the top 3.

    `credit_order IS NOT NULL` is not redundant beside `< 3`: TMDB leaves `order` off the long
    tail of a cast list, and NULL must read as "unbilled", not as slot 0."""
    return or_(
        and_(FilmCredit.credit_type == "crew", FilmCredit.job == DIRECTOR_JOB),
        and_(
            FilmCredit.credit_type == "cast",
            FilmCredit.credit_order.is_not(None),
            FilmCredit.credit_order < LEAD_TOP_BILLED_ORDER,
        ),
    )


def _coverage_credit_clause(coverage: ColumnElement[str]) -> ColumnElement[bool]:
    """The credit cut a person follow's own `coverage` column asks for (D-43).

    Takes the column rather than a Python value so the tier is read *per follow row*, inside
    the one statement: a user following one director at `lead` and another at `all` is one
    query, not two, which is what keeps the batch passes at one statement per user."""
    return or_(
        and_(coverage == "lead", lead_credit_clause()),
        and_(coverage == "all", seed_grade_credit_clause()),
    )


def _int_follows(
    entity_type: str, *, user_id: UUID | None = None, entity_id: str | None = None
) -> Subquery:
    """Follows of one integer-keyed type, with `entity_id` cast out as `key` and the whole row
    beside it — `user_id`, `coverage`, `created_at` — for the branches that need more than the
    id.

    `user_id` is optional because `covered_by_any_user_clause` asks the same question of the
    whole table: the provider poll wants "does *anybody* cover this film", and spelling those
    branches a second time is how they would come to disagree with the per-user ones. The cast
    is in the SELECT list, guarded in the WHERE, per the module docstring."""
    stmt = select(
        cast(Follow.entity_id, Integer).label("key"),
        Follow.user_id.label("user_id"),
        Follow.entity_id.label("entity_id"),
        Follow.coverage.label("coverage"),
        Follow.created_at.label("created_at"),
    ).where(
        Follow.entity_type == entity_type,
        Follow.entity_id.regexp_match(_INT_ID_PATTERN),
    )
    if user_id is not None:
        stmt = stmt.where(Follow.user_id == user_id)
    if entity_id is not None:
        stmt = stmt.where(Follow.entity_id == entity_id)
    return stmt.subquery()


def _title_follows(*, user_id: UUID | None = None) -> Subquery:
    """Title follows, with the film UUID cast out as `key`. `_int_follows` for the one type
    whose id is not an integer, and so needs its own pattern and its own cast."""
    stmt = select(
        cast(Follow.entity_id, PGUUID(as_uuid=True)).label("key"),
        Follow.user_id.label("user_id"),
        Follow.entity_id.label("entity_id"),
        Follow.created_at.label("created_at"),
    ).where(
        Follow.entity_type == "title",
        Follow.entity_id.regexp_match(_UUID_PATTERN),
    )
    if user_id is not None:
        stmt = stmt.where(Follow.user_id == user_id)
    return stmt.subquery()


def _not_muted(user_id_column: ColumnElement[UUID]) -> ColumnElement[bool]:
    """ "this follow's owner has not muted the film in the enclosing query" — the per-user half
    of `covered_by_any_user_clause`, correlated to the outer `catalog.film`."""
    return ~(
        select(literal(1))
        .select_from(WatchlistDismissal)
        .where(
            WatchlistDismissal.user_id == user_id_column,
            WatchlistDismissal.film_id == Film.id,
        )
        .exists()
    )


def _person_covered_film_ids(*, user_id: UUID, entity_id: str | None = None) -> Select[tuple[UUID]]:
    """`SELECT film_credit.film_id` for the credits this user's person follows cover (D-43)."""
    follows = _int_follows("person", user_id=user_id, entity_id=entity_id)
    return (
        select(FilmCredit.film_id)
        .join(follows, follows.c.key == FilmCredit.person_id)
        .where(_coverage_credit_clause(follows.c.coverage))
    )


def _company_film_ids(user_id: UUID, *, entity_id: str | None = None) -> Select[tuple[UUID]]:
    return select(FilmProductionCompany.film_id).where(
        FilmProductionCompany.company_id.in_(
            followed_tmdb_ids(user_id, "company", entity_id=entity_id)
        )
    )


def followed_film_ids(
    *, user_id: UUID, today: date, excluded_statuses: frozenset[str]
) -> Select[tuple[UUID]]:
    """`SELECT film.id` for every film this user's follows reach on their timeline (D-11),
    minus the ones they have muted (D-45).

    Written to be used as `Film.id.in_(followed_film_ids(...))`. `correlate(None)` keeps the
    subquery standalone whatever the caller's FROM list holds: the timeline's enclosing query
    selects from `catalog.film` too, and SQLAlchemy would otherwise auto-correlate this
    subquery's own `film` to the outer one and render it without a FROM. Inside an `IN` over
    `film.id` that happens to come out to the same rows, so this is not load-bearing there — it
    is what stops the builder's meaning depending on the query it is dropped into, which is the
    whole premise of handing the same SELECT to the notify pass (NEU-1379).

    **Coverage is not read here.** A person follow reaches every seed-grade credit on the
    timeline whatever its `coverage`: the tier decides what is worth *interrupting* somebody
    for, and narrowing the timeline with it would take away the surface the user would
    otherwise catch the beat on (D-43).

    **The mute exclusion is inside the builder, not at the call sites.** D-45 as amended
    silences a muted film everywhere, and there are four consumers — the timeline route, the
    grouped feed, the digest branch of the notify pass and `digest_event_ids` — so a rule
    applied per caller would be four places for it to be forgotten.

    A user with no follows yields no rows, so the timeline of an empty follow graph is empty
    rather than the whole feed — the onboarding prompt D-12 asks for is the client's call to
    make off `total == 0`, not something this hides by falling back.
    """
    person_films = select(FilmCredit.film_id).where(
        seed_grade_credit_clause(),
        FilmCredit.person_id.in_(followed_tmdb_ids(user_id, "person")),
    )
    return (
        select(Film.id)
        .where(
            or_(
                and_(
                    Film.id.in_(person_films),
                    in_play_clause(today=today, excluded_statuses=excluded_statuses),
                ),
                Film.id.in_(_company_film_ids(user_id)),
                Film.collection_id.in_(followed_tmdb_ids(user_id, "franchise")),
                Film.id.in_(followed_film_uuids(user_id)),
            ),
            Film.id.not_in(muted_film_ids(user_id)),
        )
        .correlate(None)
    )


def covered_film_ids(
    *,
    user_id: UUID,
    today: date,
    max_age_days: int,
    only: tuple[str, str] | None = None,
) -> Select[tuple[UUID]]:
    """`SELECT film.id` for every film this user's follows cover **for alerts** (D-43).

    The timeline filter's three differences, all of them here: a person follow admits only the
    credits its `coverage` names, the person, company and franchise branches are bounded by the
    alert window rather than by in-play — a wider date bound and a status term that ends at
    `Canceled` rather than at `Released` (D-46) — and a title follow is bounded by neither, so
    it covers its film in **any** state and at any age. The user asked for that film, and one
    they put on the list the week it came out is exactly the one they are waiting on the home
    release of.

    Mutes are *not* subtracted here; `watchlist_film_ids` is that set. Kept apart because the
    want/stop service needs the unmuted answer: "does anything still cover this film" is what
    decides between muting it and answering `204` (D-1414.5).

    `only` narrows the graph to one `(entity_type, entity_id)`, dropping the other three
    branches from the statement rather than feeding them an empty id list.

    A query builder rather than a query, in this module's shape and for its reason: the batch
    halves hand it to a statement built in `pipeline_run`, where there is no session to hand it.
    `correlate(None)` keeps the subquery standalone whatever FROM list it lands in.
    """
    window = alert_window_clause(today=today, max_age_days=max_age_days)
    branches: list[ColumnElement[bool]] = []
    scoped_id = None if only is None else only[1]

    def wants(entity_type: str) -> bool:
        return only is None or only[0] == entity_type

    if wants("person"):
        branches.append(
            and_(
                Film.id.in_(_person_covered_film_ids(user_id=user_id, entity_id=scoped_id)),
                window,
            )
        )
    if wants("company"):
        branches.append(and_(Film.id.in_(_company_film_ids(user_id, entity_id=scoped_id)), window))
    if wants("franchise"):
        branches.append(
            and_(
                Film.collection_id.in_(
                    followed_tmdb_ids(user_id, "franchise", entity_id=scoped_id)
                ),
                window,
            )
        )
    if wants("title"):
        branches.append(Film.id.in_(followed_film_uuids(user_id, entity_id=scoped_id)))
    return select(Film.id).where(or_(*branches)).correlate(None)


def watchlist_film_ids(
    *,
    user_id: UUID,
    today: date,
    max_age_days: int,
) -> Select[tuple[UUID]]:
    """**The watchlist** (D-42): `covered_film_ids` minus the films this user has muted.

    One definition, one builder. `/me/calendar`, the `.ics` feed, the alert branch of the
    notify pass, the digest's slate and the weekly digest's recipient rule read this and
    nothing else — which is what makes "what is on my watchlist" a question with one answer
    now that nothing materialises it.
    """
    return covered_film_ids(
        user_id=user_id,
        today=today,
        max_age_days=max_age_days,
    ).where(Film.id.not_in(muted_film_ids(user_id)))


def covering_follows(
    *,
    user_id: UUID,
    today: date,
    max_age_days: int,
    film_id: UUID | None = None,
) -> Select[tuple[UUID, str, str, datetime]]:
    """Every `(film_id, entity_type, entity_id, created_at)` pair where one of this user's
    follows covers a film — `covered_film_ids` keeping the follow that did it.

    `GET /me/watchlist` is one query over this: group by film in Python, and each item's
    `covered_by`, `followed` and `created_at` fall out of its pairs (D-1414.5). Muted films
    come back like any other; the route marks them rather than dropping them, because a user
    looking at their watchlist is exactly who wants to un-mute one.

    `film_id` narrows it to one film, for the want/stop routes answering with the item they
    just changed — the same pairs, so the single item cannot describe itself differently from
    the way the list would.

    A union of the four branches rather than one join against an OR, because each branch
    matches through a different key and the id casts have to stay in their SELECT lists (see
    the module docstring). Only the person branch can pair one follow with one film twice — a
    director who is also top-billed holds two qualifying credits — so only it is
    `DISTINCT`, and the union is `UNION ALL`: the other three match at most one row each by
    construction, and a dedupe across the whole set would be a sort over every pair to catch
    duplicates that cannot exist.
    """
    window = alert_window_clause(today=today, max_age_days=max_age_days)
    person_follows = _int_follows("person", user_id=user_id)
    person = (
        select(
            FilmCredit.film_id.label("film_id"),
            literal("person").label("entity_type"),
            person_follows.c.entity_id.label("entity_id"),
            person_follows.c.created_at.label("created_at"),
        )
        .select_from(FilmCredit)
        .join(person_follows, person_follows.c.key == FilmCredit.person_id)
        .join(Film, Film.id == FilmCredit.film_id)
        .where(_coverage_credit_clause(person_follows.c.coverage), window)
        .distinct()
    )
    company_follows = _int_follows("company", user_id=user_id)
    company = (
        select(
            FilmProductionCompany.film_id,
            literal("company"),
            company_follows.c.entity_id,
            company_follows.c.created_at,
        )
        .select_from(FilmProductionCompany)
        .join(company_follows, company_follows.c.key == FilmProductionCompany.company_id)
        .join(Film, Film.id == FilmProductionCompany.film_id)
        .where(window)
    )
    franchise_follows = _int_follows("franchise", user_id=user_id)
    franchise = (
        select(
            Film.id,
            literal("franchise"),
            franchise_follows.c.entity_id,
            franchise_follows.c.created_at,
        )
        .select_from(Film)
        .join(franchise_follows, franchise_follows.c.key == Film.collection_id)
        .where(window)
    )
    title_follows = _title_follows(user_id=user_id)
    title = (
        select(Film.id, literal("title"), title_follows.c.entity_id, title_follows.c.created_at)
        .select_from(Film)
        .join(title_follows, title_follows.c.key == Film.id)
    )
    pairs = union_all(person, company, franchise, title).subquery("covering")
    stmt = select(
        pairs.c.film_id, pairs.c.entity_type, pairs.c.entity_id, pairs.c.created_at
    ).correlate(None)
    return stmt if film_id is None else stmt.where(pairs.c.film_id == film_id)


def covered_by_any_user_clause(*, today: date, max_age_days: int) -> ColumnElement[bool]:
    """WHERE predicate over `catalog.film`: **somebody** covers this film and has not muted it.

    The provider and video polls' rule 2 (D-1414.3). The same four branches as
    `covered_film_ids`, spelled from the same helpers and with the same alert window, asked of
    the whole follow table instead of one user's rows — which is what stops the poll from
    reading a set the alerts do not, or the other way round.

    That window admits a `Released` film until the date bound's far end (D-46), so a film
    somebody reached through its director keeps being polled through the months its streaming
    debut actually lands in. That is the whole point of polling it; `Released` is where the
    answer arrives, not where it stops being worth asking.

    The mute test is per *covering user*, not per film: a film ten people follow and one of
    them has muted is still owed a poll, because the other nine are waiting on it. Only when
    every user who covers it has muted it does it leave the set.
    """
    window = alert_window_clause(today=today, max_age_days=max_age_days)
    person_follows = _int_follows("person")
    person = (
        select(literal(1))
        .select_from(FilmCredit)
        .join(person_follows, person_follows.c.key == FilmCredit.person_id)
        .where(
            FilmCredit.film_id == Film.id,
            _coverage_credit_clause(person_follows.c.coverage),
            _not_muted(person_follows.c.user_id),
        )
        .exists()
    )
    company_follows = _int_follows("company")
    company = (
        select(literal(1))
        .select_from(FilmProductionCompany)
        .join(company_follows, company_follows.c.key == FilmProductionCompany.company_id)
        .where(
            FilmProductionCompany.film_id == Film.id,
            _not_muted(company_follows.c.user_id),
        )
        .exists()
    )
    franchise_follows = _int_follows("franchise")
    franchise = (
        select(literal(1))
        .select_from(franchise_follows)
        .where(
            franchise_follows.c.key == Film.collection_id,
            _not_muted(franchise_follows.c.user_id),
        )
        .exists()
    )
    title_follows = _title_follows()
    title = (
        select(literal(1))
        .select_from(title_follows)
        .where(title_follows.c.key == Film.id, _not_muted(title_follows.c.user_id))
        .exists()
    )
    return or_(and_(window, or_(person, company, franchise)), title)


def events_naming_followed_people(user_id: UUID) -> Select[tuple[UUID]]:
    """`SELECT event.id` for every event whose stories name a *resolved* person this user follows
    — D-11's second half (NEU-1365) — on a film they have not muted (D-45).

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

    **The mute does reach here**, which is the case D-45 turns on: an event that only *names* a
    followed person on a muted film is still an event about that film, and "not interested in
    this film" has to mean it. That is the one exclusion this builder and `followed_film_ids`
    both carry, and it is why the join to `news.event` exists at all.

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
        .join(Event, Event.id == EventStory.event_id)
        .where(
            EventStory.story_id.in_(resolved_mentions),
            Event.film_id.not_in(muted_film_ids(user_id)),
        )
        .correlate(None)
    )
