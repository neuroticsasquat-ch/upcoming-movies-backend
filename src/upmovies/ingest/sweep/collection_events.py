"""The sweep's franchise phase: turn TMDB collection membership into events (EF-5, NEU-1434).

The studio half (`ingest.sweep.company_events`) had to build its own history table first,
because `catalog.film_production_company` is delete-and-rebuilt on every ingest. The franchise
half does not: `film.collection_id` is a plain `catalog.film` column outside
`FILM_FIELD_CHANGE_DENYLIST`, so `catalog.film_field_change` has been recording every change to
it since the trigger shipped. This phase is a reader over rows that already exist — which is
also why it needs no migration and no backfill.

Built to `ingest.sweep.company_events`' shape, deliberately, so a studio follow and a franchise
follow deliver the same kind of thing (EF-3). Everything below is either that shape unchanged
or a consequence of `collection_id` being a **scalar column** rather than a set:

- **One row can be two beats.** `NULL -> id` is an arrival, `id -> NULL` a departure, and a
  *move* `id -> id'` is one departure from the old franchise and one arrival at the new one —
  the only transition in the sweep that cards twice from a single history row.
  `collection_field_events` is that mapping and nothing else.
- **Live state is one value, not a membership test.** A change agrees with the catalog when
  `film.collection_id` equals the id an arrival names, or differs from the one a departure
  names. A film that moved on to a third franchise still agrees with the departure from the
  first, which is correct: it really has left.
- **No burst check.** The studio half withholds a company that reaches too many films in one
  observation day (D-8's shape). There is no franchise analogue worth having: filing a slate
  of films under a newly created collection in one editing session is exactly what a
  conscientious TMDB editor does, so the signature the check looks for is the *normal* case
  here rather than the suspicious one. Omitted rather than tuned to a number that would never
  fire.

Otherwise identical to the studio half, and for its reasons: both directions wait out the
credit quarantine (D-3, `SWEEP_CREDIT_QUARANTINE_HOURS`) with a live-state check at
publication; one card per (film, event type) per pass (D-7); `rumored` confidence, because
ADR-0002 makes TMDB the record for its own *scalar* fields and a collection assignment is an
editor's claim about what a film belongs to rather than a fact about the film; a departure card
supersedes the arrival card it corrects (D-2); and `Event.subject_key` carries
`collection:<tmdb_id>` tokens.

**Reverts are paired in both directions**, which is where this departs from the studio half.
That phase pairs a `removed` row with an earlier `added` one, so an attachment reverted inside
the window publishes nothing from either side. A scalar field flaps the other way just as
readily — an editor files a film under the wrong franchise, or clears the field, and puts it
back — and the far side of *that* revert is an arrival at a franchise the film never visibly
left. `mark_window_reverts` therefore pairs both directions, and `_uncarded_changes` drops the
second half of a round trip whichever way round it happened.

Contract with the pipeline conventions, matching the other phases: one session per item so a
failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures, and **no `finalize_run`** — all phases share one `ingest_run` row.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Collection, Film
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.field_events import COLLECTION_FIELD, load_change_backlog
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.news.catalog_events import (
    COLLECTION_ATTACHED_EVENT_TYPE,
    COLLECTION_EVENT_TYPES,
    COLLECTION_REMOVED_EVENT_TYPE,
)
from upmovies.news.models import Event
from upmovies.news.subject_key import collection_ids_in, collection_subject_token
from upmovies.synthesize.deterministic import (
    CollectionAttached,
    CollectionDetached,
    CollectionsAttached,
    CollectionsDetached,
    write_deterministic_summary,
)

log = logging.getLogger(__name__)

# The two directions a `collection_id` change resolves into, named rather than spelled as a
# bool so the pairing and grouping code reads the same way the studio half's does.
COLLECTION_ADDED = "added"
COLLECTION_REMOVED = "removed"

# Which event type each direction cards as. The dict is the registration: a third direction
# would fail to subscript it rather than card as something arbitrary.
COLLECTION_CHANGE_EVENT_TYPES: dict[str, str] = {
    COLLECTION_ADDED: COLLECTION_ATTACHED_EVENT_TYPE,
    COLLECTION_REMOVED: COLLECTION_REMOVED_EVENT_TYPE,
}


def collection_field_events(old_value: object, new_value: object) -> tuple[tuple[str, int], ...]:
    """The `(direction, collection_id)` pairs one `collection_id` field change becomes. Pure —
    no DB, no clock.

    Four transitions, three of which are beats:

    - `NULL -> id` — one `added`. The film joined a franchise.
    - `id -> NULL` — one `removed`. The film left one.
    - `id -> id'` — one `removed` naming the old franchise and one `added` naming the new,
      **in that order**, so a card pair rendered together reads as a move rather than as two
      unrelated beats.
    - anything else (`NULL -> NULL`, `id -> id`, a non-integer on either side) — nothing.

    The trigger writes both sides as JSONB, so an absent value arrives as `None` and a present
    one as an `int`. A value of any other type is data this function was not written against
    and is dropped rather than guessed at, on `classify_field_change`'s rule — `bool` included,
    since it is an `int` subclass and a `True` here would card as collection 1.
    """
    old = old_value if isinstance(old_value, int) and not isinstance(old_value, bool) else None
    new = new_value if isinstance(new_value, int) and not isinstance(new_value, bool) else None
    if old == new:
        return ()
    events: list[tuple[str, int]] = []
    if old is not None:
        events.append((COLLECTION_REMOVED, old))
    if new is not None:
        events.append((COLLECTION_ADDED, new))
    return tuple(events)


@dataclass(frozen=True)
class CollectionChangeRow:
    """One direction of one `collection_id` field change, with its collection named."""

    film_id: UUID
    collection_id: int
    name: str
    change: str
    changed_at: datetime
    reverted_in_window: bool = False
    """Whether this film's history holds an earlier *opposite* change for this collection
    inside the rolling window — set by `mark_window_reverts`.

    It is what tells a *round trip* from a real beat. A film filed under a franchise since its
    baseline has no `added` row at all, so its departure is a complete beat on its own; a film
    that joined inside the window and left again has one, and if that arrival never published
    the departure is a card about something no reader was ever told. The mirror holds for an
    arrival that undoes a departure nobody saw."""

    @property
    def event_type(self) -> str:
        return COLLECTION_CHANGE_EVENT_TYPES[self.change]


@dataclass(frozen=True)
class CollectionGroup:
    """The collection changes one sweep pass collapsed for one film and one event type, and the
    single event they card as."""

    film_id: UUID
    event_type: str
    changes: tuple[CollectionChangeRow, ...]
    """In the order the history produced them, oldest first — which for a scalar column is the
    order the film actually moved through the franchises, and so the order a body naming more
    than one should read them in. The studio half sorts by name instead because its changes are
    a set with no inherent order; here there is one."""

    @property
    def changed_at(self) -> datetime:
        """The *latest* `changed_at` in the group, which the event stores as `occurred_at`
        (D-7) — the credit and studio halves' rule and its reasoning in full: a burst is news
        when its last member landed, and a pass collapsing a strictly larger burst lands on a
        strictly later timestamp, so it never collides with the card the smaller burst already
        wrote under `uq_event_catalog_change`."""
        return max(c.changed_at for c in self.changes)


@dataclass
class CollectionEventResult:
    """What one collection-events pass read and wrote."""

    changes_read: int = 0
    """Counted in *directions*, not in history rows: a move is two changes to work through and
    reporting it as one would make the held and carded numbers beside it unreadable."""
    events_created: int = 0
    skipped: int = 0
    """Groups that were already carded by an earlier pass over the same rolling window. The
    steady state, not a problem."""
    held: int = 0
    """Change rows the quarantine gate withheld this pass (D-3): still inside the window, or
    already reverted and so never true. Read against `changes_read` and **not** as a health
    signal, for the reason `CreditEventResult.held` documents — in steady state the reverted
    rows dominate it, which is the gate working."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


async def load_collection_backlog(
    session: AsyncSession, *, since: datetime
) -> list[CollectionChangeRow]:
    """Every collection arrival and departure recorded at or after `since`, oldest first.

    A fixed rolling window rather than a watermark, for the reason every carding phase here
    uses one: a watermark would advance past changes a *failed* sweep never carded, losing them
    permanently, and the re-read is free because a carded group is skipped.

    Names are resolved in one query against `catalog.collection`. A collection id with no row
    there cannot be named, so its change is dropped with a log line rather than carded as a
    body with a hole in it — the FK on `film.collection_id` means this can only ever be a
    *departed* id whose collection row was deleted out from under the history.
    """
    rows = await load_change_backlog(session, since=since, fields=(COLLECTION_FIELD,))
    directed: list[tuple[UUID, str, int, datetime]] = [
        (row.film_id, change, collection_id, row.changed_at)
        for row in rows
        for change, collection_id in collection_field_events(row.old_value, row.new_value)
    ]
    if not directed:
        return []
    stmt = select(Collection.id, Collection.name).where(
        Collection.id.in_({collection_id for _, _, collection_id, _ in directed})
    )
    names: dict[int, str] = {
        collection_id: name for collection_id, name in await session.execute(stmt)
    }
    backlog: list[CollectionChangeRow] = []
    for film_id, change, collection_id, changed_at in directed:
        name = names.get(collection_id)
        if name is None:
            log.info("collection %s on film %s has no catalog row; skipped", collection_id, film_id)
            continue
        backlog.append(
            CollectionChangeRow(
                film_id=film_id,
                collection_id=collection_id,
                name=name,
                change=change,
                changed_at=changed_at,
            )
        )
    return backlog


def mark_window_reverts(changes: list[CollectionChangeRow]) -> list[CollectionChangeRow]:
    """Stamp every change that undoes an earlier opposite change for the same (film,
    collection) in this window. Pure; order preserved.

    Computed over the **whole backlog**, before the gate runs, because the change this asks
    about is usually one quarantine is *holding* — that is the entire case it exists for. A
    revert writes two rows, and the gate judges each on live state alone: the first is withheld
    because the catalog no longer agrees with it, and the second sails through for the very
    same reason. Live state cannot tell those two rows apart, so the pairing has to be read off
    the history.

    Both directions, unlike the studio half, which pairs only a removal against an earlier
    attachment. A scalar column flaps in both directions as readily, and an arrival at a
    franchise the film was never visibly seen to leave is the same unpublishable correction a
    departure from one it was never seen to join is.
    """
    seen: set[tuple[UUID, int, str]] = set()
    marked: list[CollectionChangeRow] = []
    for change in sorted(changes, key=lambda c: c.changed_at):
        key = (change.film_id, change.collection_id)
        opposite = COLLECTION_ADDED if change.change == COLLECTION_REMOVED else COLLECTION_REMOVED
        marked.append(replace(change, reverted_in_window=(*key, opposite) in seen))
        seen.add((*key, change.change))
    return marked


async def _present_collections(
    session: AsyncSession, *, film_ids: set[UUID]
) -> dict[UUID, int | None]:
    """The collection `catalog.film` holds right now for each film asked about. One query for
    the whole backlog. A film absent from the result is absent from the catalog, and reads as
    holding nothing."""
    if not film_ids:
        return {}
    stmt = select(Film.id, Film.collection_id).where(Film.id.in_(film_ids))
    return {film_id: collection_id for film_id, collection_id in await session.execute(stmt)}


async def quarantine_collection_changes(
    session: AsyncSession,
    *,
    changes: list[CollectionChangeRow],
    now: datetime,
    quarantine_hours: int,
) -> tuple[list[CollectionChangeRow], list[CollectionChangeRow]]:
    """Split the backlog into the changes eligible to card and the ones still held (D-3).
    Returns `(eligible, held)`.

    Two conditions, both required. The window must be fully observed —
    `changed_at + quarantine_hours <= now` — **and** live state must still agree with the
    change: an arrival's collection still on the film, a departure's collection still off it.
    The second is what makes this a quarantine rather than a delay: a franchise assignment
    reverted inside the window is not a beat that happened late but one that never happened,
    and it must publish nothing at all.

    Both conditions are re-evaluated from scratch on every pass, because both are properties of
    *now* rather than of the row. No `pending` state is written anywhere: the rolling window is
    the queue, which is why the hold has to fit inside it with room for the pass that observes
    it (`validate_sweep_configuration`, NEU-1401).

    `0` disables both conditions together, reverting to immediate carding — the setting is one
    switch, and holding a change for no time while still requiring live state to agree with it
    would be a third behaviour nobody asked for.
    """
    if quarantine_hours <= 0 or not changes:
        return changes, []
    hold = timedelta(hours=quarantine_hours)
    aged: list[CollectionChangeRow] = []
    held: list[CollectionChangeRow] = []
    for change in changes:
        # `<=`, so a change lands the pass it turns eligible rather than the one after.
        (aged if change.changed_at + hold <= now else held).append(change)
    if not aged:
        return [], held
    present = await _present_collections(session, film_ids={c.film_id for c in aged})
    eligible: list[CollectionChangeRow] = []
    for change in aged:
        holds_it = present.get(change.film_id) == change.collection_id
        if holds_it == (change.change == COLLECTION_ADDED):
            eligible.append(change)
        else:
            held.append(change)
    return eligible, held


def group_collection_changes(changes: list[CollectionChangeRow]) -> list[CollectionGroup]:
    """One group — and so one event — per (film, event type) in this pass. Pure.

    Burst collapsing (D-7), on `group_company_changes`' terms: the caller passes exactly the
    changes this pass will card, so "the pass" needs no key of its own, and `changed_at` is
    therefore not part of the key. A film that joined one franchise and left another in the
    same TMDB edit — which is what a move is — cards one arrival and one departure.

    One row per `(collection, observation)` survives, which matters for the same reason it does
    on the studio side: a film that joined a franchise, left it and joined it again has two
    `added` changes in one backlog, and they are two beats rather than a duplicate — so the key
    keeps the observation, and whether the second shares a body with the first is
    `_uncarded_changes`' question, asked later and per collection.
    """
    groups: dict[tuple[UUID, str], list[CollectionChangeRow]] = {}
    for change in changes:
        groups.setdefault((change.film_id, change.event_type), []).append(change)
    return [
        CollectionGroup(
            film_id=film_id,
            event_type=event_type,
            changes=_one_per_collection_per_observation(
                sorted(grouped, key=lambda c: (c.changed_at, c.collection_id))
            ),
        )
        for (film_id, event_type), grouped in groups.items()
    ]


def _one_per_collection_per_observation(
    changes: list[CollectionChangeRow],
) -> tuple[CollectionChangeRow, ...]:
    """The first change per `(collection, changed_at)`, order preserved — see
    `group_collection_changes`."""
    seen: set[tuple[int, datetime]] = set()
    kept: list[CollectionChangeRow] = []
    for change in changes:
        key = (change.collection_id, change.changed_at)
        if key not in seen:
            seen.add(key)
            kept.append(change)
    return tuple(kept)


async def _already_carded(
    session: AsyncSession, *, film_id: UUID, event_type: str, changed_at: datetime
) -> bool:
    """Whether this exact observation already has its card — the fast path under
    `uq_event_catalog_change`, whose triple this is."""
    carded = exists().where(
        Event.film_id == film_id,
        Event.event_type == event_type,
        Event.provenance == "catalog",
        Event.occurred_at == changed_at,
    )
    return bool((await session.execute(select(carded))).scalar())


async def _latest_collection_event_types(session: AsyncSession, *, film_id: UUID) -> dict[int, str]:
    """Per-collection latest event type among the two collection types, keyed by collection id.
    One query, no per-collection roundtrips."""
    stmt = (
        select(Event.subject_key, Event.event_type)
        .where(
            Event.film_id == film_id,
            Event.event_type.in_(COLLECTION_EVENT_TYPES),
            Event.subject_key.isnot(None),
        )
        .order_by(Event.occurred_at.desc(), Event.created_at.desc())
    )
    latest: dict[int, str] = {}
    for subject_key, event_type in await session.execute(stmt):
        for collection_id in collection_ids_in(subject_key):
            latest.setdefault(collection_id, event_type)
    return latest


async def _uncarded_changes(
    session: AsyncSession,
    *,
    film_id: UUID,
    event_type: str,
    changes: tuple[CollectionChangeRow, ...],
) -> tuple[CollectionChangeRow, ...]:
    """The changes in a group that should still be carded for this beat.

    State-aware, the way `company_events._uncarded_changes` is: for each collection, the most
    recent card among the two collection types says what the reader was last told, and the
    change is suppressed only when that already says what this group would say. *The latest
    card for a collection reflects the film's current membership of it* is the invariant, and
    it is what makes join → leave → re-join read as three cards while a re-read of the same
    rolling window adds none.

    **A change that undoes one the reader was never told about is suppressed too.** A
    collection with no card on this film at all is either one the film has been filed under
    since its baseline — in which case its departure is a complete beat, which is why there is
    no prior-arrival-card gate here — or one it joined or left inside this window without that
    publishing, which is what a reverted edit looks like from the far side.
    `reverted_in_window` separates the two, and only the second is dropped: carding it would
    announce a move that, as far as every reader can see, undoes nothing.

    The gate is deliberately conditioned on there being *no* card rather than on the revert
    alone. A film whose genuine departure was carded and which TMDB then re-files under the
    same franchise has `reverted_in_window` set on the arrival, and that arrival is real news
    the reader is owed — its prior card says `collection_removed`, so it survives here.
    """
    latest = await _latest_collection_event_types(session, film_id=film_id)
    kept: list[CollectionChangeRow] = []
    for change in changes:
        carded_as = latest.get(change.collection_id)
        if carded_as == event_type:
            continue
        if carded_as is None and change.reverted_in_window:
            continue
        kept.append(change)
    return tuple(kept)


async def supersede_prior_collection_cards(session: AsyncSession, *, removal: Event) -> int:
    """Mark the arrival card each collection named on `removal` was current on (D-2).

    Per collection on the removal's `subject_key`: the most recent *published*
    `collection_attached` card that occurred before the removal is set `superseded` with
    `superseded_by` pointing at the removal. Only the most recent one — an older card the same
    collection is on was already the earlier claim, not the one this removal corrects. Nothing
    is hidden or deleted; the card keeps its place on every surface.

    The card, not the collection, is the unit of supersession: `status` lives on the event row,
    so a card naming two franchises is marked when the film leaves either.

    Returns the number of cards marked. Caller owns the commit; `removal` must be flushed so
    its id exists for the FK.
    """
    # Resolve every target before marking any, for `supersede_prior_company_cards`' reason:
    # marking inside the loop would autoflush the first UPDATE ahead of the next collection's
    # query, and a card two departed collections share would then fail the `published` filter
    # for the second — handing back an older card that collection is on.
    targets: dict[UUID, Event] = {}
    for collection_id in collection_ids_in(removal.subject_key):
        token = collection_subject_token(collection_id)
        stmt = (
            select(Event)
            .where(
                Event.film_id == removal.film_id,
                Event.event_type == COLLECTION_ATTACHED_EVENT_TYPE,
                Event.subject_key.any(token),  # pyright: ignore[reportArgumentType]
                Event.status == "published",
                Event.occurred_at < removal.occurred_at,
            )
            .order_by(Event.occurred_at.desc(), Event.created_at.desc())
            .limit(1)
        )
        card = (await session.execute(stmt)).scalar_one_or_none()
        if card is not None:
            targets[card.id] = card
    for card in targets.values():
        card.status = "superseded"
        card.superseded_by = removal.id
    marked = len(targets)
    await session.flush()
    return marked


async def _card_group(session: AsyncSession, *, group: CollectionGroup) -> bool:
    """Create the event and its deterministic summary for one group, or report that it was
    already carded. One transaction covers both writes — and, for a departure, the supersession
    marks on the arrival cards it corrects — so a card can never reach the feed without the
    summary row every read path inner-joins, nor a departure with its original still reading
    `published`. Caller owns the commit.

    Per-collection suppression runs **before** the already-carded check, and the card is dated
    by what survives it: under burst collapsing the group's latest `changed_at` can belong to a
    collection the card will not name, and dating the card by the whole group would both
    misdate it and let it collide with a card that already holds that timestamp.
    """
    changes = await _uncarded_changes(
        session, film_id=group.film_id, event_type=group.event_type, changes=group.changes
    )
    if not changes:
        return False
    occurred_at = max(c.changed_at for c in changes)
    if await _already_carded(
        session, film_id=group.film_id, event_type=group.event_type, changed_at=occurred_at
    ):
        return False
    attached = group.event_type == COLLECTION_ATTACHED_EVENT_TYPE
    event = Event(
        film_id=group.film_id,
        event_type=group.event_type,
        confidence="rumored",
        provenance="catalog",
        occurred_at=occurred_at,
        region=None,
        subject_key=[collection_subject_token(c.collection_id) for c in changes],
    )
    session.add(event)
    await session.flush()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=(
            CollectionsAttached(collections=tuple(CollectionAttached(name=c.name) for c in changes))
            if attached
            else CollectionsDetached(
                collections=tuple(CollectionDetached(name=c.name) for c in changes)
            )
        ),
        source_updated_at=event.updated_at,
    )
    if not attached:
        await supersede_prior_collection_cards(session, removal=event)
    return True


async def run_collection_events(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    now: datetime,
    lookback_days: int,
    quarantine_hours: int = 0,
    failure_threshold: int = 10,
) -> CollectionEventResult:
    """Card every franchise arrival and departure TMDB recorded in the window, less the ones
    quarantine is still holding.

    `quarantine_hours` defaults to off — to 0 rather than the setting's 72 — the same way the
    other carding phases' gates do: the caller that has a `Settings` passes the tuned value,
    and every other caller gets the plain behaviour it was written against. No burst threshold,
    unlike the studio half, and no TMDB client, unlike the credit attachment phase; the module
    docstring says why for both.
    """
    result = CollectionEventResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)
    since = now - timedelta(days=lookback_days)

    async with owned_session(session_factory) as s:
        backlog = mark_window_reverts(await load_collection_backlog(s, since=since))
        # Same session as the load: the gate reads live `film.collection_id` against the
        # backlog just read, and a second session could straddle a refresh that moved it.
        eligible, held = await quarantine_collection_changes(
            s, changes=backlog, now=now, quarantine_hours=quarantine_hours
        )
    # The whole backlog, not what survived the gate: "read 40, carded 2, held 38" is the shape
    # that says the phase is working, and netting those rows out of the read count would make a
    # quarantine that holds everything look like an empty window.
    result.changes_read = len(backlog)
    result.held = len(held)
    groups = group_collection_changes(eligible)
    log.info(
        "collection events: %d changes in %d groups since %s (%d held, quarantine %dh)",
        result.changes_read,
        len(groups),
        since.isoformat(),
        result.held,
        quarantine_hours,
    )

    for group in groups:
        await heartbeat.tick()
        try:
            async with owned_session(session_factory) as s:
                created = await _card_group(s, group=group)
                if created:
                    await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except IntegrityError:
            # `uq_event_catalog_change` is the structural backstop under the skip check above,
            # so reaching it means a concurrent writer got there first — the group *is* carded.
            log.info(
                "collection group %s/%s was carded concurrently", group.film_id, group.event_type
            )
            result.skipped += 1
            guard.succeeded()
            continue
        except Exception:
            # One unwritable event must not cost the rest of the backlog.
            log.exception("carding collections for film %s failed", group.film_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
                log.error("collection events: %s", result.abort_error)
                return result
            continue
        guard.succeeded()
        if created:
            result.events_created += 1
        else:
            result.skipped += 1

    log.info(
        "collection events: %d created, %d already carded, %d held, %d failed",
        result.events_created,
        result.skipped,
        result.held,
        result.failures,
    )
    return result
