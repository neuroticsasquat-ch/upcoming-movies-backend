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

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CompoundSelect,
    DateTime,
    Integer,
    Select,
    Text,
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
from upmovies.news.models import (
    RESOLVED_MENTION_PATHS,
    Event,
    EventStory,
    EventSummary,
    StoryPerson,
)
from upmovies.news.subject_key import (
    COLLECTION_SUBJECT_PREFIX,
    COMPANY_SUBJECT_PREFIX,
    sql_normalized_name,
)
from upmovies.news.visibility import feed_visible

_INT_ID_PATTERN = r"^[0-9]+$"
_UUID_PATTERN = r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"

_INT32_MAX = "2147483647"
"""The largest value the catalog's TMDB keys (`Integer`, i.e. Postgres int4) hold, as the digit
string `_int_id_guard` compares against — see there for why it is compared as text.

`follow_repo._INT32_MAX` is the same bound as an `int`, for the Python-side half of the check.
Spelled twice rather than shared because the two are different types for different comparisons,
and importing the repo here would put a repo on the batch passes' import path."""

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


def _int_id_guard(entity_id: Any) -> ColumnElement[bool]:
    """`entity_id` is a decimal integer the catalog's `Integer` keys can actually hold.

    The digit guard on its own is not enough, and `follow_repo._entity_key` names the hole this
    closes: an id past int32 passes `^[0-9]+$` and `normalise_entity_id`'s positive-integer
    check, then fails **in the driver** as it is bound against an `Integer` column. That is not
    one row losing its name, it is the user's whole follows list 500ing on behalf of one bad
    row — the state a Letterboxd or TMDB import can leave behind (D-15, D-16). It was a batch
    pass's problem until `follow_last_activity` put these builders on `GET /me/follows`.

    Compared as **text**, by length and then lexically, because the cast is the very thing being
    guarded and a numeric comparison would have to perform it first. Digit strings of equal
    length order the same way as the numbers they spell, so the two-part test is exact rather
    than a conservative digit-count bound — a nine-digit ceiling would be simpler and would
    silently drop a valid id the day TMDB passes a billion."""
    return (
        entity_id.regexp_match(_INT_ID_PATTERN)
        & (func.length(entity_id) <= len(_INT32_MAX))
        & ((func.length(entity_id) < len(_INT32_MAX)) | (entity_id <= _INT32_MAX))
    )


def followed_tmdb_ids(
    user_id: UUID, entity_type: str, *, entity_id: str | None = None
) -> Select[tuple[int]]:
    """The TMDB ids this user follows under `entity_type`, or just `entity_id` when one is named.

    `entity_id` narrows the graph to one row for the callers that ask about a single follow —
    the want/stop service asking "does anything *else* cover this film" — so they cost one
    branch's work instead of four.

    The shape guard (`_int_id_guard`) sits beside the `entity_type` filter in the same SELECT,
    so the projected cast only ever sees a row of this type whose value is an integer the
    catalog's keys can hold, and a row that is neither is skipped rather than failing the
    statement (see the module docstring).

    Belt and braces on top of `app.dto.normalise_entity_id`, which is what *should* keep a
    non-numeric id out of the table — but it is a boundary rule, applied by the follow routes'
    request models, while `follow_service.follow` takes an `entity_id` straight from its caller
    and the imports (D-15, D-16) are such callers."""
    stmt = select(cast(Follow.entity_id, Integer)).where(
        Follow.user_id == user_id,
        Follow.entity_type == entity_type,
        _int_id_guard(Follow.entity_id),
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

    The cast is in the SELECT list and the shape guard in the WHERE, per the module docstring.
    """
    return (
        select(cast(Follow.entity_id, Integer))
        .where(
            Follow.entity_type == entity_type,
            _int_id_guard(Follow.entity_id),
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


def _int_id_source(
    entity_type: str, *, user_id: UUID | None, entity_id: str | None
) -> Select[tuple[int]]:
    """The TMDB ids a branch covers, as integers: this user's follows of `entity_type`, or the
    single entity a public entity page names (`user_id is None`).

    The one place the two callers of every branch below differ. `/me/follows` asks "what do
    *my* follows deliver"; `GET /people/{ref}/events` asks "what does *this entity's* stream
    hold", for a visitor who may have no account at all (EF-18) — and the cards are the same
    cards, so the rule has to be the same rule. Swapping the id set at the bottom is what makes
    it one rule rather than two that drift."""
    if user_id is not None:
        return followed_tmdb_ids(user_id, entity_type, entity_id=entity_id)
    # `entity_event_ids` is the only caller that passes no user, it takes an `int`, and it
    # always names one. Asserted rather than defaulted: a `None` here would silently build a
    # branch selecting entity 0's cards, which is a page quietly showing the wrong thing.
    assert entity_id is not None
    return select(cast(literal(int(entity_id)), Integer))


def _text_id_source(entity_type: str, *, user_id: UUID | None, entity_id: str | None) -> Subquery:
    """`_int_id_source` as the follow graph spells ids — text — for the two branches that build
    a `subject_key` token by concatenation rather than casting to a catalog key.

    A subquery rather than a `Select` because those branches *join* it: the token they match is
    `'company:' || entity_id`, which is not a column of anything."""
    if user_id is None:
        return select(cast(literal(entity_id), Text).label("entity_id")).subquery("named_entity")
    stmt = select(Follow.entity_id.label("entity_id")).where(
        Follow.user_id == user_id,
        Follow.entity_type == entity_type,
        _int_id_guard(Follow.entity_id),
    )
    if entity_id is not None:
        stmt = stmt.where(Follow.entity_id == entity_id)
    return stmt.subquery("followed_organisations")


def _pair(entity_type: str, entity_id: Any) -> list[Any]:
    """The four columns every branch below projects: which entity this card belongs to, the
    card, and its `created_at`.

    `cast` on both key columns so the `UNION ALL` that stacks the branches sees one type per
    position — a bind parameter in a union arm is `unknown` to Postgres otherwise, and the two
    id columns come variously from `catalog` integer keys and `app.follow`'s text."""
    return [
        cast(literal(entity_type), Text).label("entity_type"),
        cast(entity_id, Text).label("entity_id"),
        Event.id.label("event_id"),
        Event.created_at.label("created_at"),
    ]


def entity_attachment_event_ids(
    user_id: UUID, *, only: tuple[str, str] | None = None
) -> Select[tuple[UUID]]:
    """`SELECT event.id` for every published card this user's **person, studio and franchise**
    follows deliver (EF-3, EF-13).

    Written to be used as `Event.id.in_(entity_attachment_event_ids(u))`, OR-ed against
    `title_follow_film_ids` rather than folded into it: an attachment card makes *that event*
    timeline-worthy and says nothing about the rest of the film's history.

    Five branches, UNION-ed (`_entity_event_pairs`):

    - `_person_attachment_pairs` — `casting` / `crew_attached` / `credit_removed` cards whose
      `subject_key` names a followed person (D-1437.3);
    - `_organisation_attachment_pairs` twice — `company_attached` / `company_removed` and
      `collection_attached` / `collection_removed` cards carrying a followed id token
      (D-1437.4);
    - `first_association_clause` (two arms) — the story-backed attach and detach cards whose
      resolved mentions make a followed person's first association with the film (D-1437.5);
    - `_canceled_pairs` — the film's `canceled` card, for every follower of an entity currently
      attached to it (D-1437.6).

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

    `only` narrows the graph to one `(entity_type, entity_id)` — what `follow_last_activity`
    needs for a single row, and it costs one `if` per branch.
    """
    pairs = _entity_event_pairs(user_id=user_id, only=only)
    if pairs is None:
        return _no_events()
    reached = pairs.subquery("entity_attachment")
    return select(Event.id).where(Event.id.in_(select(reached.c.event_id))).correlate(None)


def entity_event_ids(entity_type: str, entity_id: int) -> Select[tuple[UUID]]:
    """`SELECT event.id` for one entity's own attach, detach and `canceled` cards, with nobody
    following anything (EF-18).

    What an entity *page* lists, for a signed-out visitor: the same five branches
    `entity_attachment_event_ids` unions, with the follow graph swapped out for the one id the
    URL names (`_int_id_source`, `_text_id_source`). That is the point of routing both through
    `_entity_event_pairs` — the page's promise is "this is what following delivers", and a page
    built from its own second spelling of the rule is a promise that goes stale the first time
    the rule moves.

    `entity_type` is the **follow** graph's word, so a franchise is `franchise` here and
    `collection` in the catalog and in its `subject_key` token (CONTEXT.md **Franchise**).
    Visibility stays the caller's, as above.
    """
    pairs = _entity_event_pairs(user_id=None, only=(entity_type, str(entity_id)))
    if pairs is None:
        return _no_events()
    reached = pairs.subquery("entity_events")
    return select(Event.id).where(Event.id.in_(select(reached.c.event_id))).correlate(None)


def _entity_event_pairs(
    *, user_id: UUID | None, only: tuple[str, str] | None
) -> CompoundSelect[tuple[str, str, UUID, datetime]] | None:
    """`(entity_type, entity_id, event_id, created_at)` for every card an entity follow
    delivers — the shape every consumer in this module reduces.

    Rows rather than ids because the follows page asks the attributing question the timeline
    never does: *which* follow did this card arrive through (EF-15). `entity_attachment_event_ids`
    throws the key away and keeps the ids; `follow_last_activity` keeps the key and takes a
    `max` per row; `entity_event_ids` fixes the key and keeps the ids.

    `None` when `only` names a type no branch here owns — `title`, whose follows select films —
    so the caller can answer with `_no_events()` rather than union nothing.
    """

    def wants(entity_type: str) -> bool:
        return only is None or only[0] == entity_type

    scoped_id = None if only is None else only[1]
    branches: list[
        Select[tuple[str, str, UUID, datetime]] | CompoundSelect[tuple[str, str, UUID, datetime]]
    ] = []
    if wants("person"):
        branches.append(_person_attachment_pairs(user_id=user_id, entity_id=scoped_id))
        first_association = first_association_clause(user_id=user_id, only=only)
        if first_association is not None:
            branches.append(first_association)
    if wants("company"):
        branches.append(
            _organisation_attachment_pairs("company", user_id=user_id, entity_id=scoped_id)
        )
    if wants("franchise"):
        branches.append(
            _organisation_attachment_pairs("franchise", user_id=user_id, entity_id=scoped_id)
        )
    branches.extend(_canceled_pairs(user_id=user_id, only=only))
    if not branches:
        return None
    return union_all(*branches)


def _person_attachment_pairs(
    *, user_id: UUID | None, entity_id: str | None
) -> Select[tuple[str, str, UUID, datetime]]:
    """The person branch: every published credit attach or detach card naming one of the people
    in scope, keyed to the person it names.

    `credit_removed` cards name the removed person exactly as the attach cards name the
    arriving one — the sweep's removal path writes `subject_key` from the same
    `normalize_name` — so the three types are one test rather than two.

    A join on `sql_normalized_name(person.name) = ANY(event.subject_key)` where this used to
    hold an `EXISTS` of the same test, because the key has to come *out*. It is the same
    nested loop over the same small id set, and where the `EXISTS` collapsed a card naming two
    followed people to one row, the join emits the two rows the attribution needs. A NULL
    `subject_key` — every card that is not about a person — never matches either way.

    Why not `followed_people()`: that builder is system-wide and has no user. The per-user set
    is `followed_tmdb_ids`, which already carries the shape guard."""
    return (
        select(*_pair("person", Person.id))
        .select_from(Event)
        .join(Person, sql_normalized_name(Person.name) == any_(Event.subject_key))
        .where(
            Event.status == _PUBLISHED,
            Event.event_type.in_(PERSON_ATTACHMENT_EVENT_TYPES),
            Person.id.in_(_int_id_source("person", user_id=user_id, entity_id=entity_id)),
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


def _organisation_attachment_pairs(
    entity_type: str, *, user_id: UUID | None, entity_id: str | None
) -> Select[tuple[str, str, UUID, datetime]]:
    """The studio and franchise branches (D-1437.4): an exact match on the id token a card
    carries, which is what makes these two branches lossless where the person branch is not.

    The shape guard on the follow rows is belt and braces here rather than load-bearing —
    nothing is cast, so a malformed `entity_id` would build a token that matches nothing rather
    than abort the statement — and is kept so that every id branch in this module reads the
    same way."""
    prefix, event_types = _ORGANISATION_BRANCHES[entity_type]
    followed = _text_id_source(entity_type, user_id=user_id, entity_id=entity_id)
    return (
        select(*_pair(entity_type, followed.c.entity_id))
        .select_from(Event)
        .join(followed, literal(prefix).concat(followed.c.entity_id) == any_(Event.subject_key))
        .where(
            Event.status == _PUBLISHED,
            Event.event_type.in_(event_types),
        )
        .correlate(None)
    )


def _canceled_pairs(
    *, user_id: UUID | None, only: tuple[str, str] | None
) -> list[Select[tuple[str, str, UUID, datetime]]]:
    """The `canceled` branches (D-1437.6): a film being called off reaches every follower of an
    entity **currently attached** to it — a credit of any kind, a production-company row, the
    film's collection.

    Read at query time, on purpose. EF-6 chose `canceled` *because* TMDB rarely strips credits
    from a cancelled film, so "currently attached" is the durable answer; and a follow created
    after the cancellation still finds the card, which is right — a director's cancelled film is
    part of their stream. Title followers reach the same card through the film term, and the
    union's de-duplication makes that one row.

    Three selects where this used to be one with an `or_` of three `EXISTS`, because each
    attachment table carries a *different* entity key and the key is now projected. A list, so
    a scope naming a type with no branch here contributes nothing rather than an `or_()` of
    nothing.

    A person credited twice on one film (a writer-director) yields the same pair twice. Both
    consumers fold it — `entity_attachment_event_ids` de-duplicates on the primary key,
    `follow_last_activity` takes a `max` — so there is no `DISTINCT` here to pay for.
    """

    def wants(entity_type: str) -> bool:
        return only is None or only[0] == entity_type

    scoped_id = None if only is None else only[1]

    def canceled(key: Any, entity_type: str, joined: Any, on: Any) -> Any:
        return (
            select(*_pair(entity_type, key))
            .select_from(Event)
            .join(joined, on)
            .where(Event.status == _PUBLISHED, Event.event_type == CANCELED_EVENT_TYPE)
            .correlate(None)
        )

    branches: list[Select[tuple[str, str, UUID, datetime]]] = []
    if wants("person"):
        branches.append(
            canceled(
                FilmCredit.person_id,
                "person",
                FilmCredit,
                (FilmCredit.film_id == Event.film_id)
                & FilmCredit.person_id.in_(
                    _int_id_source("person", user_id=user_id, entity_id=scoped_id)
                ),
            )
        )
    if wants("company"):
        branches.append(
            canceled(
                FilmProductionCompany.company_id,
                "company",
                FilmProductionCompany,
                (FilmProductionCompany.film_id == Event.film_id)
                & FilmProductionCompany.company_id.in_(
                    _int_id_source("company", user_id=user_id, entity_id=scoped_id)
                ),
            )
        )
    if wants("franchise"):
        branches.append(
            canceled(
                Film.collection_id,
                "franchise",
                Film,
                (Film.id == Event.film_id)
                & Film.collection_id.in_(
                    _int_id_source("franchise", user_id=user_id, entity_id=scoped_id)
                ),
            )
        )
    return branches


def first_association_clause(
    *, user_id: UUID | None, only: tuple[str, str] | None = None
) -> CompoundSelect[tuple[str, str, UUID, datetime]] | None:
    """The story-backed attach and detach cards whose resolved mentions make an entity's
    **first association** with, or first detachment from, the film (EF-13), as
    `_entity_event_pairs` rows — or `None` when `only` names a type this clause does not own.

    A story mention is not an attachment — nobody has joined anything until TMDB says so — so a
    person follow cannot simply take every card whose story names them: an interview, a festival
    piece and the fourth outlet to run the same casting would each be a timeline row. What the
    product promises is the *news*: the day the trades say somebody has signed on to a film they
    were not on before. That is this clause, and everything else a story says about them is
    nothing to their followers.

    **One builder, two arms, and M4 extends it in place** (NEU-1446): the `story_entity` arm for
    studios and franchises goes *inside* this function, beside the person arm, because the
    timeline and the notify pass both reach the rule through here — and so, now, does every
    entity page (EF-18) — and a second builder beside it is how they would come to disagree.

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

    **A card can appear more than once**, keyed to a different person each time, and twice for
    one person when two of its stories mention them. The arms cannot repeat a row between them
    — they are disjoint by `event_type` — and neither consumer minds: the pairs *are* the
    attribution EF-15 wants, and the id readers de-duplicate on the primary key
    (`first_association_event_ids`, `entity_attachment_event_ids`).
    """
    if only is not None and only[0] != "person":
        return None
    entity_id = None if only is None else only[1]
    return union_all(
        _first_association_arm(user_id=user_id, entity_id=entity_id),
        _first_detachment_arm(user_id=user_id, entity_id=entity_id),
    )


def first_association_event_ids(
    *, user_id: UUID, only: tuple[str, str] | None = None
) -> Select[tuple[UUID]]:
    """`SELECT DISTINCT event.id` over `first_association_clause` — the clause as the readers
    that only ask *whether* a card qualifies want it.

    `DISTINCT` because the clause keys each row to the person it names, and a caller counting
    timeline rows wants the card once however many followed people a story mentioned.

    **No production caller today** — `entity_attachment_event_ids` unions the clause's rows and
    de-duplicates once at the top, so the only readers are the tests that assert EF-13's rule on
    its own. Kept rather than inlined into them: the projection is this module's business, and a
    test that spelled `select(distinct(...))` itself would be the second spelling the module
    exists to prevent. M4 (NEU-1446) is the caller it is waiting for."""
    pairs = first_association_clause(user_id=user_id, only=only)
    if pairs is None:
        return _no_events()
    arms = pairs.subquery("first_association")
    return select(arms.c.event_id).distinct().correlate(None)


def _mentioning_pairs(
    *,
    user_id: UUID | None,
    entity_id: str | None,
    card_types: tuple[str, ...],
    mention_types: tuple[str, ...],
    mention: type[StoryPerson],
    person: type[Person],
    extra: list[ColumnElement[bool]],
) -> Select[tuple[str, str, UUID, datetime]]:
    """Published cards of `card_types` carrying a resolved mention, typed as one of
    `mention_types`, of a person in scope — keyed to that person, with `extra` narrowing.

    `RESOLVED_MENTION_PATHS` is the cut (D-25): `accepted` and `tiebreak` match, `unlinked` and
    `not_in_tmdb` never do. A mention nobody was named in carries `person_id` NULL and drops out
    of the `IN` on its own, which is why the path filter needs no null guard beside it — and why
    the two cannot be collapsed into "has a `person_id`": an `unlinked` row written with a
    candidate id would then match.

    `person` is joined in for its name, which the "does an earlier card name them" terms in
    `extra` compare against a card's `subject_key`, and for the key this projects. The mention,
    its story and its person are joined rather than held inside an `EXISTS` for the reason
    `_person_attachment_pairs` gives: the key has to come out."""
    return (
        select(*_pair("person", person.id))
        .select_from(Event)
        .join(EventStory, EventStory.event_id == Event.id)
        .join(mention, mention.story_id == EventStory.story_id)
        .join(person, person.id == mention.person_id)
        .where(
            Event.status == _PUBLISHED,
            Event.event_type.in_(card_types),
            mention.path.in_(RESOLVED_MENTION_PATHS),
            mention.person_id.in_(_int_id_source("person", user_id=user_id, entity_id=entity_id)),
            mention.features["event_type"].astext.in_(mention_types),
            *extra,
        )
        .correlate(None)
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


def _first_association_arm(
    *, user_id: UUID | None, entity_id: str | None
) -> Select[tuple[str, str, UUID, datetime]]:
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
    return _mentioning_pairs(
        user_id=user_id,
        entity_id=entity_id,
        card_types=card_types,
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
    )


def _first_detachment_arm(
    *, user_id: UUID | None, entity_id: str | None
) -> Select[tuple[str, str, UUID, datetime]]:
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
    return _mentioning_pairs(
        user_id=user_id,
        entity_id=entity_id,
        card_types=(CREDIT_REMOVED_EVENT_TYPE,),
        mention_types=STORY_DETACH_MENTION_TYPES,
        mention=mention,
        person=person,
        extra=[~detached_since],
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


def follow_last_activity(
    user_id: UUID, *, only: tuple[str, str] | None = None
) -> Select[tuple[str, str, datetime]]:
    """`(entity_type, entity_id, last_activity_at)` — the `created_at` of the newest visible
    card each of this user's follows delivers (EF-15), for every follow that has delivered one.

    **One statement for the whole page, not one per row.** The follows page sorts by last
    activity, so the column is needed for every row before the first one can be drawn; an
    imported library is hundreds of follows, and a query each would be hundreds of round trips
    for a sort key. A follow that has delivered nothing is simply absent from the result, and
    the caller reads it as NULL — a `GROUP BY` cannot invent a row for a group with no members,
    and the alternative (an outer join from `app.follow`) would buy a row of NULLs at the cost
    of the aggregate's index.

    Two arms, because the two kinds of follow reach cards by different grains — the same split
    the timeline spells, read at the other end:

    - a **title** follow's activity is any visible beat on its film (`_title_follows`, EF-3),
      so this arm is a join from the follow row to `news.event` on `film_id`;
    - a **person, studio or franchise** follow's activity is the cards
      `entity_attachment_event_ids` would deliver, attributed back to the follow they came
      through (`_entity_event_pairs`).

    **Visibility is applied here**, unlike in the two builders above, which leave it to the
    query they are dropped into: this *is* the final query. `feed_visible()` plus the summary
    join, so the date on the follows page is the date of a card the user can actually open.

    `status = 'published'` on both arms. The entity branches carry it themselves (a superseded
    attach card is not a beat to deliver, D-2), and the title arm matches them rather than the
    feed, which still renders a superseded card in place: the two arms disagreeing about what
    counts as activity is the drift this module exists to prevent, and a superseded card is
    superseded *by* a later card on the same film, so the newest date is unchanged either way.

    `only` narrows to one `(entity_type, entity_id)`, for the follow routes that answer with a
    single row — the same seam, and the same reason, as on the builders above.
    """
    arms: list[Select[tuple[str, str, datetime]]] = []

    if only is None or only[0] == "title":
        title_follows = _title_follows(user_id=user_id)
        title = (
            select(
                cast(literal("title"), Text).label("entity_type"),
                cast(title_follows.c.entity_id, Text).label("entity_id"),
                Event.created_at.label("created_at"),
            )
            .select_from(title_follows)
            .join(Event, Event.film_id == title_follows.c.key)
            .join(Film, Film.id == Event.film_id)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .where(Event.status == _PUBLISHED, *feed_visible())
        )
        if only is not None:
            title = title.where(title_follows.c.entity_id == only[1])
        arms.append(title)

    pairs = _entity_event_pairs(user_id=user_id, only=only)
    if pairs is not None:
        reached = pairs.subquery("delivered")
        arms.append(
            select(
                reached.c.entity_type,
                reached.c.entity_id,
                Event.created_at.label("created_at"),
            )
            .select_from(reached)
            .join(Event, Event.id == reached.c.event_id)
            .join(Film, Film.id == Event.film_id)
            .join(EventSummary, EventSummary.event_id == Event.id)
            .where(*feed_visible())
        )

    if not arms:
        # `only` named a type the follow graph does not hold. No arm, so no group, so no row —
        # which the caller already reads as "nothing yet".
        return select(
            cast(literal(""), Text).label("entity_type"),
            cast(literal(""), Text).label("entity_id"),
            cast(literal(None), DateTime(timezone=True)).label("last_activity_at"),
        ).where(false())

    delivered = union_all(*arms).subquery("activity")
    return select(
        delivered.c.entity_type,
        delivered.c.entity_id,
        func.max(delivered.c.created_at).label("last_activity_at"),
    ).group_by(delivered.c.entity_type, delivered.c.entity_id)


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
