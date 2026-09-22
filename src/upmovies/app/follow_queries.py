"""The follow graph read as a *filter* — what a user's follows deliver.

Beside `app/entitlements.py` and for the same reason: the callers are not all here yet. The
timeline (`public.service.get_timeline`) asks this today; the notify pass (NEU-1379) asks the
same question of every user at once, in SQL, because a batch pass cannot lean on a route
dependency. So these are **query builders, not queries**: each takes a `user_id` and returns a
`Select` (or a WHERE predicate), holds no session, and reads nothing request-scoped — that is
what makes them callable from `pipeline_run` as well as from a route.

**A title follow selects films; an entity follow selects events (EF-3, ADR-0019).** That is the
whole shape of the module now, and it replaced D-11's "every film any follow reaches":

- `title_follow_film_ids` — the films this user asked for by name, in any state. A title follow
  delivers every published beat on its film.
- `entity_attachment_event_ids` — the *cards* this user's person, studio and franchise follows
  deliver: the attach and detach cards naming that entity, plus the `canceled` card of a film it
  is attached to. Nothing else about those films. Following a director is an interest in what
  they sign on to, not a subscription to the trailer of everything they have ever made.

The timeline, the digest and the alert branch all spell the same clause over the two:

    Event.film_id IN title_follow_film_ids(u)  OR  Event.id IN entity_attachment_event_ids(u)

`get_timeline` hands them to `get_feed_grouped` as `film_filter` and `event_filter`, which OR-s
the two grains itself; the notify pass OR-s them in its own statement. Reusing the builders
rather than restating the rule is the point — a digest that quietly covered less than the
timeline it summarises is the drift this module exists to prevent.

**Neither kind of follow has a window, an in-play term or a mute** (EF-14). An entity follow
selects events, published now, one per attachment, so there is nothing to bound; a title follow
selects the film the user named, in any state and at any age. The alert window is gone from this
module with `covered_film_ids` and the computed watchlist it fed (D-42, D-43). It survives in
`catalog.queries` for the one question that is still about films and dates — which films an
entity page lists as recently released, and which an import may propose (EF-21) — and the
provider poll's own date window is rule 1's, spelled in `ingest.providers` and never this
module's. `app.watchlist_dismissal` is dropped, so there
is no set that subtracts: the correction to a list of titles you followed by name is unfollowing
one.

**A followed person is matched to a card by normalized name, in SQL (D-1437.3).**
`Event.subject_key` carries `normalize_name(person.name)` tokens and never person ids, and the
sweep does not stamp `carded_by_event_id` on the rows it cards itself — so there is no id-level
link from a catalog credit card to its person, and the person branch compares
`news.subject_key.sql_normalized_name` against the tokens. Companies and franchises are matched
on their `company:<id>` / `collection:<id>` tokens instead, which is exact. The name match has
two known misses, both accepted and documented on `sql_normalized_name`: a name whose case
folds differently in Python and in SQL, and a person TMDB has since renamed.

**Every id cast is spelled in a SELECT list, never in a WHERE.** `entity_id` is polymorphic
text, so the three integer types have to be cast back to meet the catalog's keys — and Postgres
does not promise to evaluate `entity_type = 'person'` before the cast beside it. A row of the
wrong shape would then not degrade a filter, it would abort the statement: the user's whole
timeline would 500, and the batch passes would fail for every user over one bad row. Projecting
the cast means the WHERE (the type filter and the shape guard) has already run when it happens.
"""

from uuid import UUID

from sqlalchemy import (
    Integer,
    Select,
    any_,
    cast,
    false,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from upmovies.app.models import Follow
from upmovies.catalog.models import (
    Film,
    FilmCredit,
    FilmCreditChange,
    FilmProductionCompany,
    Person,
)
from upmovies.news.catalog_events import (
    CANCELED_EVENT_TYPE,
    COLLECTION_EVENT_TYPES,
    COMPANY_EVENT_TYPES,
    CREDIT_EVENT_TYPES,
    CREDIT_REMOVED_EVENT_TYPE,
    PERSON_ATTACHMENT_EVENT_TYPES,
)
from upmovies.news.models import RESOLVED_MENTION_PATHS, Event, EventStory, StoryPerson
from upmovies.news.subject_key import (
    COLLECTION_SUBJECT_PREFIX,
    COMPANY_SUBJECT_PREFIX,
    sql_normalized_name,
)

_INT_ID_PATTERN = r"^[0-9]+$"
_UUID_PATTERN = r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"

_PUBLISHED = "published"

STORY_ATTACH_MENTION_TYPES: tuple[str, ...] = ("casting",)
"""The `story_person.features->>'event_type'` values that name somebody in connection with an
*attachment* (EF-13).

One member, and not `crew_attached`: the cluster vocabulary (`link.cluster._VALID_TYPES`) has no
such type, so a director signing on comes back classified `casting` and `casting` is the whole
attach vocabulary on the mention side. Spelled as a constant here — rather than imported from
`link.cluster`, which would pull the LLM gateway and the TMDB client onto a public read path, and
rather than inlined — so a prompt that widens the vocabulary, and M4's organisation arm, have one
place to widen."""

STORY_DETACH_MENTION_TYPES: tuple[str, ...] = ()
"""The mention `event_type` values that name somebody in connection with a *detachment*.

**Empty at M3, deliberately.** The story vocabulary has no detach type at all, so no
story-backed detach card exists and `first_association_clause`'s detach arm selects nothing. The
arm is spelled anyway, and pinned empty by a test: it is the seam M4 and any later prompt change
fill, and the contract of this ticket is that both arms live in one builder rather than one
arriving later beside it."""

_ATTACH_CARD_TYPES: tuple[str, ...] = tuple(sorted(CREDIT_EVENT_TYPES))
"""The card types a person *attaching* to a film can be, ordered so the rendered SQL is stable.
`CREDIT_EVENT_TYPES` is a frozenset, and an `IN` whose member order changes between processes
makes two identical statements look different in a log or a plan cache."""

_CREDIT_CHANGE_ADDED = "added"
"""`ingest.tmdb.credit_history.CREDIT_ADDED`, spelled rather than imported: that module imports
`followed_people` from this one, so the import would be a cycle."""


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


def _followed_by_anyone(entity_type: str) -> Select[tuple[int]]:
    """`SELECT DISTINCT` the TMDB ids **somebody** follows under one integer-keyed type.

    The system-wide counterpart to `followed_tmdb_ids`, and the shared body of the three
    builders below. One spelling rather than three, on `_int_follows`' reasoning: the ingest
    path asks this of all three types now (EF-4), and three copies of the same SELECT are three
    chances for one of them to lose the digit guard.

    The cast is in the SELECT list and the digit guard in the WHERE, per the module docstring.
    """
    return (
        select(cast(Follow.entity_id, Integer))
        .where(
            Follow.entity_type == entity_type,
            Follow.entity_id.regexp_match(_INT_ID_PATTERN),
        )
        .distinct()
    )


def followed_people() -> Select[tuple[int]]:
    """`SELECT DISTINCT person_id` for every person **somebody** follows (EF-2).

    One of the three builders in this module that ask nothing about a user: the callers are
    batch passes deciding what the *system* records and enumerates, not what one person sees.
    The credit history (D-49) records a non-seed credit change when its person is in this set,
    the sweep (D-50) enumerates these people beside the seed set, and the admission exception
    (EF-4, D-1436.1) writes an `added` row for their credits on a film's first observation.

    Every live person follow, with no tier to filter on since EF-1 made the follow binary —
    which is what makes "recorded grade" a set the user can reason about: follow someone, and
    every credit they take is written down.

    **No entitlement filter**, on `covered_by_any_user_clause`'s reasoning: D-40 keeps a lapsed
    user's follows, and the poll set does not filter either. Recording a credit change for
    somebody whose grant has lapsed costs one row and is exactly what should already be there
    when they come back; dropping it would need a backfill that cannot be written, because the
    observation is gone.
    """
    return _followed_by_anyone("person")


def followed_companies() -> Select[tuple[int]]:
    """`SELECT DISTINCT company_id` for every production company **somebody** follows (EF-4).

    `followed_people` for studios, and read for one reason only: a film admitted with a
    followed studio already on it records a `film_company_change` rather than a baseline
    (D-1436.2). There is no recorded grade for companies — every company crossing the set is
    written down whoever follows it — so nothing else in the ingest path asks.

    No entitlement filter and no user, for the reason `followed_people` gives.
    """
    return _followed_by_anyone("company")


def followed_franchises() -> Select[tuple[int]]:
    """`SELECT DISTINCT collection_id` for every franchise **somebody** follows (EF-4).

    `followed_companies` for collections, read at the same point and for the same reason: a
    film *inserted* into a followed franchise records the `film_field_change` row the
    `BEFORE UPDATE` trigger could not write (D-1436.4).

    The follow's `entity_type` is `franchise` and the catalog column is `collection_id` — the
    glossary's two words for one thing, and the reason this builder is named for the product
    concept and returns the catalog's ids.

    No entitlement filter and no user, for the reason `followed_people` gives.
    """
    return _followed_by_anyone("franchise")


def _title_follows(*, user_id: UUID | None = None) -> Subquery:
    """Title follows, with the film UUID cast out as `key` and the whole row beside it.

    The one follow type whose id is not an integer, so it needs its own pattern and its own
    cast. `user_id` is optional because `title_followed_by_any_user_clause` asks the same
    question of the whole table: the provider poll wants "is *anybody* waiting on this film",
    and spelling that branch a second time is how it would come to disagree with the per-user
    one."""
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


def title_follow_film_ids(user_id: UUID) -> Select[tuple[UUID]]:
    """`SELECT film.id` for every film this user follows **by title**, in any state.

    Written to be used as `Film.id.in_(title_follow_film_ids(u))` — or, on the timeline and in
    the notify pass, as `Event.film_id.in_(...)`, which is safe because `news.event.film_id` is
    NOT NULL. A title follow delivers every published beat on its film (EF-3), so this is the
    whole of the film half of the clause: no in-play term, no alert window, no status cut. The
    user asked for that film, and one they followed the week it came out is exactly the one
    they are waiting on the home release of (EF-14).

    A person, company or franchise follow puts **nothing** here. That is the cutover: those
    follows select events, through `entity_attachment_event_ids`, and widening this builder to
    cover them is what would pull a followed director's whole slate back onto the timeline.

    Nothing subtracts from it either (EF-14). The mute used to, and the film it silenced was
    one the user had not chosen — reached through a followed director — which is a state that
    can no longer arise: this set holds exactly what the user asked for by name, so the way to
    leave it is to unfollow.

    `correlate(None)` for the reason the module docstring gives: the timeline's enclosing query
    selects from `catalog.film` too, and SQLAlchemy would otherwise auto-correlate this
    subquery's own `film` to the outer one and render it without a FROM.
    """
    return select(Film.id).where(Film.id.in_(followed_film_uuids(user_id))).correlate(None)


def follow_reach(user_id: UUID) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """The two halves of `follow_scope`, kept apart: `(via a title follow, via an entity
    follow)` over `news.event`.

    Every reader that only asks *whether* a card reaches this user wants them OR-ed, and that
    is `follow_scope`. The notify pass's **alert** branch asks the harder question EF-7 poses —
    *why* the card reached them — because the push sets differ by reach: a 12th-billed casting
    card is digest-only for the film's follower and an interrupt for the performer's, and the
    same row can be both at once for a user who follows the two. So it selects these as two
    labelled booleans beside the event and decides per reach in Python.

    Returned as a pair rather than as two builders because they are one decomposition and
    reading one without the other is how the OR silently loses a term."""
    return (
        Event.film_id.in_(title_follow_film_ids(user_id)),
        Event.id.in_(entity_attachment_event_ids(user_id)),
    )


def follow_scope(user_id: UUID) -> ColumnElement[bool]:
    """WHERE predicate over `news.event`: this user's follows deliver this card (EF-3).

    The clause the module docstring states, spelled once for the readers that need it as a
    predicate rather than as two builders — the notify pass's alert and digest branches. The
    timeline instead hands `title_follow_film_ids` and `entity_attachment_event_ids` to
    `get_feed_grouped`, which OR-s them into the same shape itself, because its scope has to
    apply to four statements (the day count, the day window, the film-day rows and the event
    fetch) rather than one.

    `or_` over `follow_reach` rather than its own pair of `IN`s, so the scope and the reach can
    never come to different answers about what a follow delivers — the alert branch decides
    per reach (EF-7) and the digest branch over the whole scope, and the two must agree that a
    card reaching nobody reaches neither.

    Lives here, beside the two builders, so there is one place to look for "what does a follow
    deliver" — NEU-1440 reads the builders under it, and a clause with a home in
    `notify_service` is one they would each re-spell."""
    return or_(*follow_reach(user_id))


def _no_events() -> Select[tuple[UUID]]:
    """`SELECT event.id` selecting nothing — what a branch set narrowed to a type it does not
    own comes to. A statement rather than an empty id list, so a caller composing `IN` against
    it gets valid SQL, and `false()` rather than `or_()` of nothing, which SQLAlchemy refuses."""
    return select(Event.id).where(false()).correlate(None)


def entity_attachment_event_ids(
    user_id: UUID, *, only: tuple[str, str] | None = None
) -> Select[tuple[UUID]]:
    """`SELECT event.id` for every published card this user's **person, studio and franchise**
    follows deliver (EF-3, EF-13).

    Written to be used as `Event.id.in_(entity_attachment_event_ids(u))`, OR-ed against
    `title_follow_film_ids` rather than folded into it: an attachment card makes *that event*
    timeline-worthy and says nothing about the rest of the film's history.

    Five branches, UNION-ed:

    - `_person_attachment_events` — `casting` / `crew_attached` / `credit_removed` cards whose
      `subject_key` names a followed person (D-1437.3);
    - `_organisation_attachment_events` twice — `company_attached` / `company_removed` and
      `collection_attached` / `collection_removed` cards carrying a followed id token
      (D-1437.4);
    - `first_association_clause` — the story-backed attach and detach cards whose resolved
      mentions make a followed person's first association with the film (D-1437.5);
    - `_canceled_for_attached_entities` — the film's `canceled` card, for every follower of an
      entity currently attached to it (D-1437.6).

    Each branch reads `news.event` with `status = 'published'` and the event types it owns, and
    nothing else: the feed's own visibility terms (`visible_events()`, `region_visible()`, the
    slug term, the summary join) stay in the query this is dropped into, exactly as D-11's
    mention builder left them. A superseded attach card (D-2) is therefore never selected; its
    detach card is, and it names the same entity.

    **The union is de-duplicated**, because an event can be reached by more than one branch — a
    user following both the film's studio and its director sees one `canceled` card, and a
    catalog casting card matched by name is matched again by a resolved mention of the story
    that promoted it. Spelled as a `UNION ALL` of the branches wrapped in an `IN`, which
    de-duplicates on the primary key once at the top rather than five times over.

    Nothing subtracts from the result (EF-14): the mute this used to exclude is gone with the
    watchlist it corrected, and it never fitted here anyway — a mute silenced a *film*, while
    these are cards about an entity that happen to name one.

    `only` narrows the graph to one `(entity_type, entity_id)`. Nothing in this ticket passes
    it; it is the seam NEU-1440's `last_activity_at` needs — "the newest card that reaches this
    user through *this* follow" — and it costs one `if` per branch.
    """

    def wants(entity_type: str) -> bool:
        return only is None or only[0] == entity_type

    scoped_id = None if only is None else only[1]
    branches: list[Select[tuple[UUID]]] = []
    if wants("person"):
        branches.append(_person_attachment_events(user_id, entity_id=scoped_id))
        branches.append(first_association_clause(user_id=user_id, only=only))
    if wants("company"):
        branches.append(_organisation_attachment_events(user_id, "company", entity_id=scoped_id))
    if wants("franchise"):
        branches.append(_organisation_attachment_events(user_id, "franchise", entity_id=scoped_id))
    canceled = _canceled_for_attached_entities(user_id, only=only)
    if canceled is not None:
        branches.append(canceled)
    if not branches:
        return _no_events()
    reached = union_all(*branches).subquery("entity_attachment")
    return select(Event.id).where(Event.id.in_(select(reached.c.id))).correlate(None)


def _names_a_followed_person(user_id: UUID, *, entity_id: str | None = None) -> ColumnElement[bool]:
    """EXISTS predicate: one of the enclosing `news.event`'s `subject_key` tokens is the
    normalized name of a person this user follows (D-1437.3).

    `= ANY(event.subject_key)` rather than an overlap against a constructed array, which is the
    same test one row at a time and reads as the question being asked. A NULL `subject_key` —
    every card that is not about a person — yields NULL and so never matches.

    Why not `followed_people()`: that builder is system-wide and has no user. The per-user set
    is `followed_tmdb_ids`, which already carries the digit guard."""
    return (
        select(literal(1))
        .select_from(Person)
        .where(
            Person.id.in_(followed_tmdb_ids(user_id, "person", entity_id=entity_id)),
            sql_normalized_name(Person.name) == any_(Event.subject_key),
        )
        .correlate(Event)
        .exists()
    )


def _person_attachment_events(
    user_id: UUID, *, entity_id: str | None = None
) -> Select[tuple[UUID]]:
    """The person branch: every published credit attach or detach card naming somebody this
    user follows.

    `credit_removed` cards name the removed person exactly as the attach cards name the
    arriving one — the sweep's removal path writes `subject_key` from the same
    `normalize_name` — so the three types are one test rather than two."""
    return (
        select(Event.id)
        .where(
            Event.status == _PUBLISHED,
            Event.event_type.in_(PERSON_ATTACHMENT_EVENT_TYPES),
            _names_a_followed_person(user_id, entity_id=entity_id),
        )
        .correlate(None)
    )


_ORGANISATION_BRANCHES: dict[str, tuple[str, tuple[str, ...]]] = {
    "company": (COMPANY_SUBJECT_PREFIX, COMPANY_EVENT_TYPES),
    "franchise": (COLLECTION_SUBJECT_PREFIX, COLLECTION_EVENT_TYPES),
}
"""The two token-matched branches: the follow's `entity_type`, the `subject_key` prefix its ids
carry, and the event types that carry them.

The follow row says `franchise` where the catalog and the token say `collection` — the
glossary's two words for one thing (CONTEXT.md **Franchise**), and the reason this mapping
exists rather than an f-string per branch. Both prefixes are read from `news.subject_key`,
where the carding paths write them, never restated."""


def _organisation_attachment_events(
    user_id: UUID, entity_type: str, *, entity_id: str | None = None
) -> Select[tuple[UUID]]:
    """The studio and franchise branches (D-1437.4): an exact match on the id token a card
    carries, which is what makes these two branches lossless where the person branch is not.

    The digit guard is belt and braces here rather than load-bearing — nothing is cast, so a
    malformed `entity_id` would build a token that matches nothing rather than abort the
    statement — and is kept so that every id branch in this module reads the same way."""
    prefix, event_types = _ORGANISATION_BRANCHES[entity_type]
    follows = select(Follow.entity_id.label("entity_id")).where(
        Follow.user_id == user_id,
        Follow.entity_type == entity_type,
        Follow.entity_id.regexp_match(_INT_ID_PATTERN),
    )
    if entity_id is not None:
        follows = follows.where(Follow.entity_id == entity_id)
    followed = follows.subquery("followed_organisations")
    return (
        select(Event.id)
        .where(
            Event.status == _PUBLISHED,
            Event.event_type.in_(event_types),
            select(literal(1))
            .select_from(followed)
            .where(literal(prefix).concat(followed.c.entity_id) == any_(Event.subject_key))
            .correlate(Event)
            .exists(),
        )
        .correlate(None)
    )


def _canceled_for_attached_entities(
    user_id: UUID, *, only: tuple[str, str] | None = None
) -> Select[tuple[UUID]] | None:
    """The `canceled` branch (D-1437.6): a film being called off reaches every follower of an
    entity **currently attached** to it — a credit of any kind, a production-company row, the
    film's collection.

    Read at query time, on purpose. EF-6 chose `canceled` *because* TMDB rarely strips credits
    from a cancelled film, so "currently attached" is the durable answer; and a follow created
    after the cancellation still finds the card, which is right — a director's cancelled film is
    part of their stream. Title followers reach the same card through the film term, and the
    union's de-duplication makes that one row.

    Returns `None` when `only` names a type with no branch here, so the caller can leave the
    branch out of the union rather than build an `or_()` of nothing."""

    def wants(entity_type: str) -> bool:
        return only is None or only[0] == entity_type

    scoped_id = None if only is None else only[1]
    attached: list[ColumnElement[bool]] = []
    if wants("person"):
        attached.append(
            select(literal(1))
            .select_from(FilmCredit)
            .where(
                FilmCredit.film_id == Event.film_id,
                FilmCredit.person_id.in_(followed_tmdb_ids(user_id, "person", entity_id=scoped_id)),
            )
            .correlate(Event)
            .exists()
        )
    if wants("company"):
        attached.append(
            select(literal(1))
            .select_from(FilmProductionCompany)
            .where(
                FilmProductionCompany.film_id == Event.film_id,
                FilmProductionCompany.company_id.in_(
                    followed_tmdb_ids(user_id, "company", entity_id=scoped_id)
                ),
            )
            .correlate(Event)
            .exists()
        )
    if wants("franchise"):
        # `correlate(Event)` and not the default: `catalog.film` is in the *enclosing* feed
        # query's FROM list, so auto-correlation would otherwise strip it from this EXISTS and
        # silently re-point the collection test at the outer film.
        attached.append(
            select(literal(1))
            .select_from(Film)
            .where(
                Film.id == Event.film_id,
                Film.collection_id.in_(
                    followed_tmdb_ids(user_id, "franchise", entity_id=scoped_id)
                ),
            )
            .correlate(Event)
            .exists()
        )
    if not attached:
        return None
    return (
        select(Event.id)
        .where(
            Event.status == _PUBLISHED,
            Event.event_type == CANCELED_EVENT_TYPE,
            or_(*attached),
        )
        .correlate(None)
    )


def first_association_clause(
    *, user_id: UUID, only: tuple[str, str] | None = None
) -> Select[tuple[UUID]]:
    """`SELECT event.id` for the story-backed attach and detach cards whose resolved mentions
    make a followed entity's **first association** with, or first detachment from, the film
    (EF-13).

    A story mention is not an attachment — nobody has joined anything until TMDB says so — so a
    person follow cannot simply take every card whose story names them: an interview, a festival
    piece and the fourth outlet to run the same casting would each be a timeline row. What the
    product promises is the *news*: the day the trades say somebody has signed on to a film they
    were not on before. That is this clause, and everything else a story says about them is
    nothing to their followers.

    **One builder, two arms, and M4 extends it in place** (NEU-1446): the `story_entity` arm for
    studios and franchises goes *inside* this function, beside the person arm, because the
    timeline and the notify pass both reach the rule through here and a second builder beside it
    is how the two would come to disagree.

    The attach arm, for a published event `E` on film `F` and a resolved mention `M` of a
    followed person `P` on one of `E`'s stories:

    1. `E` is a `casting` / `crew_attached` card and `M.features->>'event_type'` is an attach
       type (`STORY_ATTACH_MENTION_TYPES`). The card has to be an attach card *and* the mention
       has to be named in connection with the attachment beat.
    2. No earlier published attach card for `P` on `F` — by name token or by a resolved attach
       mention, the same two mechanisms the branch itself uses, so "a card names `P`" means one
       thing throughout the module. This is what makes the *second* outlet, and a split beat's
       later card, select nothing.
    3. No current credit for `P` on `F` that is not `E`'s own confirmation: either
       `film_credit` holds nothing, or a `film_credit_change` row for that credit is stamped
       `carded_by_event_id = E`. The carve-out is D-1437.5, and without it a story card that
       published first (D-5) would drop off the timeline the morning TMDB confirmed it. A
       *baseline* credit — no change row at all — still blocks: the person was on the film
       before anyone wrote about it, so the story is not the first association. A credit that
       arrived after `E` and was never stamped blocks too, and in that case the sweep raises its
       own catalog card, which the name branch selects.

    The clause deliberately does **not** re-check the mention against `E`'s `subject_key`. A
    casting card about performer X whose story also names a director, with the director's
    mention typed `casting`, is exactly the case term 3 exists for: a credited director holds a
    credit, so his followers do not see X's card. If he is not credited anywhere in our data,
    the card *is* his first association with the film as far as we know, and it reaches his
    followers once. Accepted.

    The detach arm is the mirror — a `credit_removed` card, a detach-typed mention, and no
    published `credit_removed` for `P` on `F` since the last attach card — and it selects
    nothing at M3, because `STORY_DETACH_MENTION_TYPES` is empty. See that constant.

    The two arms need no `DISTINCT` between them: each selects one row per event and they
    are disjoint by `event_type`, so the `UNION ALL` cannot repeat an id. The caller's own
    de-duplication covers the branches that *can* overlap.
    """
    if only is not None and only[0] != "person":
        return _no_events()
    entity_id = None if only is None else only[1]
    arms = union_all(
        _first_association_arm(user_id, entity_id=entity_id),
        _first_detachment_arm(user_id, entity_id=entity_id),
    ).subquery("first_association")
    return select(arms.c.id).correlate(None)


def _resolved_mentions_of(
    user_id: UUID,
    *,
    entity_id: str | None,
    mention_types: tuple[str, ...],
    mention: type[StoryPerson],
    person: type[Person],
    extra: list[ColumnElement[bool]],
) -> ColumnElement[bool]:
    """EXISTS predicate: one of the enclosing event's stories carries a resolved mention of a
    person this user follows, typed as one of `mention_types`, and `extra` holds of it.

    `RESOLVED_MENTION_PATHS` is the cut (D-25): `accepted` and `tiebreak` match, `unlinked` and
    `not_in_tmdb` never do. A mention nobody was named in carries `person_id` NULL and drops out
    of the `IN` on its own, which is why the path filter needs no null guard beside it — and why
    the two cannot be collapsed into "has a `person_id`": an `unlinked` row written with a
    candidate id would then match.

    `person` is joined in for its name, which the "does an earlier card name them" terms in
    `extra` compare against a card's `subject_key`."""
    return (
        select(literal(1))
        .select_from(EventStory)
        .join(mention, mention.story_id == EventStory.story_id)
        .join(person, person.id == mention.person_id)
        .where(
            EventStory.event_id == Event.id,
            mention.path.in_(RESOLVED_MENTION_PATHS),
            mention.person_id.in_(followed_tmdb_ids(user_id, "person", entity_id=entity_id)),
            mention.features["event_type"].astext.in_(mention_types),
            *extra,
        )
        .correlate(Event)
        .exists()
    )


def _card_names_person(
    card: type[Event],
    *,
    mention: type[StoryPerson],
    person: type[Person],
    mention_types: tuple[str, ...],
) -> ColumnElement[bool]:
    """ "`card` names this person" — the module's one answer to that question, in both of the
    ways a card can name somebody: a normalized name token on the card itself (the catalog
    path), or a resolved mention of the same person, typed as an attachment, on one of the
    card's stories (the story path).

    Both mechanisms, because either one of them is a card the follower has already seen: an
    attachment carded from TMDB's own history and one carded from a trade story are the same
    beat as far as "have I heard this before" goes."""
    prior_story = aliased(EventStory)
    prior_mention = aliased(StoryPerson)
    return or_(
        sql_normalized_name(person.name) == any_(card.subject_key),
        select(literal(1))
        .select_from(prior_story)
        .join(prior_mention, prior_mention.story_id == prior_story.story_id)
        .where(
            prior_story.event_id == card.id,
            prior_mention.person_id == mention.person_id,
            prior_mention.path.in_(RESOLVED_MENTION_PATHS),
            prior_mention.features["event_type"].astext.in_(mention_types),
        )
        .correlate(card, mention)
        .exists(),
    )


def _first_association_arm(user_id: UUID, *, entity_id: str | None) -> Select[tuple[UUID]]:
    """The attach arm of `first_association_clause` — see its docstring for the three terms."""
    mention = aliased(StoryPerson)
    person = aliased(Person)
    prior = aliased(Event)
    card_types = _ATTACH_CARD_TYPES
    mention_types = STORY_ATTACH_MENTION_TYPES
    earlier_attach_card = (
        select(literal(1))
        .select_from(prior)
        .where(
            prior.film_id == Event.film_id,
            prior.status == _PUBLISHED,
            prior.event_type.in_(card_types),
            prior.created_at < Event.created_at,
            _card_names_person(prior, mention=mention, person=person, mention_types=mention_types),
        )
        .correlate(Event, mention, person)
        .exists()
    )
    return (
        select(Event.id)
        .where(
            Event.status == _PUBLISHED,
            Event.event_type.in_(card_types),
            _resolved_mentions_of(
                user_id,
                entity_id=entity_id,
                mention_types=mention_types,
                mention=mention,
                person=person,
                extra=[
                    ~earlier_attach_card,
                    or_(
                        ~_holds_a_credit(mention),
                        _credit_carded_by_this_event(mention),
                    ),
                ],
            ),
        )
        .correlate(None)
    )


def _first_detachment_arm(user_id: UUID, *, entity_id: str | None) -> Select[tuple[UUID]]:
    """The detach arm of `first_association_clause`, which selects nothing while
    `STORY_DETACH_MENTION_TYPES` is empty — spelled in full because M4 fills that constant, and
    an arm written then is an arm written against a rule nobody is holding in their head.

    "First detachment" is "no published `credit_removed` card for this person on this film since
    the last attach card for them", not "no `credit_removed` card ever": a person who joins,
    leaves, rejoins and leaves again has detached twice, and both are news."""
    mention = aliased(StoryPerson)
    person = aliased(Person)
    attach = aliased(Event)
    removal = aliased(Event)
    latest_attach_card = (
        select(func.max(attach.created_at))
        .where(
            attach.film_id == Event.film_id,
            attach.status == _PUBLISHED,
            attach.event_type.in_(_ATTACH_CARD_TYPES),
            _card_names_person(
                attach,
                mention=mention,
                person=person,
                mention_types=STORY_ATTACH_MENTION_TYPES,
            ),
        )
        .correlate(Event, mention, person)
        .scalar_subquery()
    )
    detached_since = (
        select(literal(1))
        .select_from(removal)
        .where(
            removal.film_id == Event.film_id,
            removal.status == _PUBLISHED,
            removal.event_type == CREDIT_REMOVED_EVENT_TYPE,
            removal.created_at < Event.created_at,
            _card_names_person(
                removal,
                mention=mention,
                person=person,
                mention_types=STORY_DETACH_MENTION_TYPES,
            ),
            # No attach card at all (the `IS NULL` arm) means *every* earlier detach card
            # counts, so this one is not the first. The spec's "since the latest published
            # attach card" has nothing to measure from in that case; declining is the reading
            # that cannot double-deliver, and the sweep's removal gate already wants a prior
            # visible attach card before it cards a detachment at all (this ticket's
            # out-of-scope note). M4 inherits this, so it is spelled rather than implied.
            or_(latest_attach_card.is_(None), removal.created_at > latest_attach_card),
        )
        .correlate(Event, mention, person)
        .exists()
    )
    return (
        select(Event.id)
        .where(
            Event.status == _PUBLISHED,
            Event.event_type == CREDIT_REMOVED_EVENT_TYPE,
            _resolved_mentions_of(
                user_id,
                entity_id=entity_id,
                mention_types=STORY_DETACH_MENTION_TYPES,
                mention=mention,
                person=person,
                extra=[~detached_since],
            ),
        )
        .correlate(None)
    )


def _holds_a_credit(mention: type[StoryPerson]) -> ColumnElement[bool]:
    """EXISTS: the mentioned person holds any credit on the enclosing event's film — any credit
    type, any billing, any job, because any of them means they were already on the film."""
    return (
        select(literal(1))
        .select_from(FilmCredit)
        .where(
            FilmCredit.film_id == Event.film_id,
            FilmCredit.person_id == mention.person_id,
        )
        .correlate(Event, mention)
        .exists()
    )


def _credit_carded_by_this_event(mention: type[StoryPerson]) -> ColumnElement[bool]:
    """EXISTS: the mentioned person's attachment to the enclosing event's film was published
    *by that event* (D-5's stamp) — so the credit standing there now is this card's own
    confirmation rather than a prior attachment (D-1437.5)."""
    return (
        select(literal(1))
        .select_from(FilmCreditChange)
        .where(
            FilmCreditChange.film_id == Event.film_id,
            FilmCreditChange.person_id == mention.person_id,
            FilmCreditChange.change == _CREDIT_CHANGE_ADDED,
            FilmCreditChange.carded_by_event_id == Event.id,
        )
        .correlate(Event, mention)
        .exists()
    )


def title_followed_by_any_user_clause() -> ColumnElement[bool]:
    """WHERE predicate over `catalog.film`: **somebody** follows this film by title (EF-14).

    D-27's rule 2, and what is left of it: the provider and video polls poll a film nobody has
    a theatrical date reason to poll when a user is waiting on it, and waiting on it now means
    having followed it by name. It selects over the same follow rows `title_follow_film_ids`
    does — the same `entity_type` filter and the same UUID shape guard, asked of the whole table
    rather than of one user — so the poll cannot come to a different answer about what somebody
    is waiting on than the pass that tells them about it.

    **No window, no status term, no mute, and no arguments** — the three branches that needed
    them are gone. `covered_by_any_user_clause` had to bound its person, company and franchise
    branches by the alert window or a single followed director would have dragged a whole back
    catalogue into the poll; a title follow is one film the user named, so there is nothing to
    bound and nothing for `today` or `max_age_days` to do here. The theatrical rule beside it in
    `ingest.providers.poll_set_clause` still carries its own date window, which is where the
    poll's volume is actually decided.

    That makes this set strictly smaller than the one it replaces, and deliberately so: a film
    reached only through a followed person is no longer something anybody is waiting on, because
    no surface delivers it to them any more. Polling it would be buying offers for a card with
    no reader.
    """
    title_follows = _title_follows()
    return (
        select(literal(1)).select_from(title_follows).where(title_follows.c.key == Film.id).exists()
    )
