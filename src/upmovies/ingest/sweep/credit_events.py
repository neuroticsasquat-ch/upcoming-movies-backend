"""The sweep's fourth (and fifth) phase: turn TMDB's credit attachments and detachments into
events (ADR-0014, spec §5.2; NEU-1200 reverses the "detachments are never carded" decision).

The payoff half of catalog-sourced events, and the expensive one — `catalog.film_credit` is
delete-and-rebuilt on every ingest, so NEU-1082 had to build the history this phase reads.
It is also the one the project was largely built for: a director attaching to an undated film
is the "this is real now" beat no trade has written about yet.

**Confidence is `rumored`**, unlike the field-change phase's `confirmed`. ADR-0002 makes TMDB
the system of record for its own scalar fields, so a `release_date` move *is* the corroboration
— but a credit is community-edited, and one added by an anonymous editor is not a studio
announcement. A removal is even less authoritative.

**First observation is a baseline** (§5.3) is inherited, not re-implemented: `film_credit_change`
holds no rows at all for a film whose credits the catalog had never observed, so there is
nothing here to read. The integration tests assert it anyway — it is the failure that would be
most visible in production, and this phase is where it would surface.

**One observation is one card.** TMDB routinely gains a whole top-billed cast between two
ingests. The grouping below is the difference between one `casting` card naming three people
and three cards about one beat, and `uq_event_catalog_change` enforces the same thing
structurally: one catalog event per film, type and timestamp. Detachments share the same
grouping discipline — one `credit_removed` card per (film, changed_at).

**An attachment is quarantined before it cards** (ADR-0017, D-3). A `change='added'` row is
eligible only once it has survived `SWEEP_CREDIT_QUARANTINE_HOURS` *and* the credit is still
in `catalog.film_credit` under the same seed-grade role. TMDB is community-edited, and the
edit this suppresses is the one that was never true — vandalism reverted within the hour,
a misfiled credit — which under immediate carding published a beat and then needed a
correction card to take it back. Nothing is written while a row is held: the rolling window
*is* the queue, re-read and re-judged on every pass, which is why the hold has to stay inside
it. The live cast list is never held — state mirrors TMDB immediately, only events wait.

**A removal card supersedes the attachment card it corrects** (ADR-0017, D-2). Carding a
`credit_removed` event marks, for each person it names, their most recent published
attachment card (`crew_attached`/`casting`, any provenance, occurred before the removal)
`superseded` and points its `superseded_by` at the removal. The original stays on every
surface; the marker is the only change. A re-attachment after that is a fresh `published`
card — removal-aware suppression already lets it through — so attach → remove → re-attach
reads superseded, published, published.

Contract with the pipeline conventions, matching the other phases: one session per item
so a failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures, and **no `finalize_run`** — all phases share one `ingest_run` row.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import FilmCredit, FilmCreditChange, Person
from upmovies.catalog.seed_grade import crew_role, is_seed_grade
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.credit_history import CREDIT_ADDED, CREDIT_REMOVED
from upmovies.news.catalog_events import (
    CREDIT_EVENT_TYPES,
    CREDIT_REMOVED_EVENT_TYPE,
    CREDIT_ROLE_EVENT_TYPES,
)
from upmovies.news.models import Event
from upmovies.news.subject_key import normalize_name
from upmovies.synthesize.deterministic import (
    CreditAttached,
    CreditDetached,
    CreditsAttached,
    CreditsDetached,
    write_deterministic_summary,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttachedCredit:
    """One `change='added'` row of `catalog.film_credit_change`, with its person named."""

    film_id: UUID
    person_id: int
    name: str
    role: str
    changed_at: datetime


@dataclass(frozen=True)
class CreditGroup:
    """The credits one observation of one film attached, and the single event they card as."""

    film_id: UUID
    event_type: str
    changed_at: datetime
    credits: tuple[CreditAttached, ...]


@dataclass
class CreditEventResult:
    """What one credit-attachment pass read and wrote."""

    attachments_read: int = 0
    events_created: int = 0
    skipped: int = 0
    """Groups that were already carded — by an earlier pass over the same rolling window, or
    by a trade story that reported the attachment first. The steady state, not a problem."""
    held: int = 0
    """Attachments the quarantine gate withheld this pass (D-3): still inside the window, or
    already gone from `catalog.film_credit` and so never true. Not a failure and not a skip —
    a held row is re-read on every pass until it either cards or falls out of the rolling
    window.

    The two reasons are deliberately one number, and it is therefore not a health signal: a
    reverted row is withheld on every pass until it ages out, so in steady state it dominates
    the count, and a high `held` reads as quarantine doing its job rather than as a window set
    too long. Splitting them is `ingest.credit_hold`'s job (D-8), which logs a reason per held
    item and is where a per-reason count belongs."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


@dataclass(frozen=True)
class DetachedCredit:
    """One `change='removed'` row of `catalog.film_credit_change`, with its person named."""

    film_id: UUID
    person_id: int
    name: str
    role: str
    changed_at: datetime


@dataclass(frozen=True)
class DetachmentGroup:
    """The credits one observation of one film detached, and the single event they card as."""

    film_id: UUID
    changed_at: datetime
    credits: tuple[DetachedCredit, ...]


@dataclass
class CreditDetachmentResult:
    """What one credit-detachment pass read and wrote."""

    detachments_read: int = 0
    events_created: int = 0
    skipped: int = 0
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


def credit_role(credit_type: str, job: str | None) -> str | None:
    """The seed-grade role one credit-change row carries, or None when it carries none.

    Reads the same predicate the history was written against (`catalog.seed_grade`), so a
    role this returns is always one the summary templates have a clause for.
    """
    if credit_type == "cast":
        return "cast"
    if credit_type == "crew":
        return crew_role(job)
    return None


def group_attachments(attachments: list[AttachedCredit]) -> list[CreditGroup]:
    """One group — and so one event — per (film, observation, event type). Pure.

    Keyed on `changed_at` rather than on the run: the timestamp is what `occurred_at` records
    and what `uq_event_catalog_change` keys on, so two observations stay two cards however
    close together the sweep reads them.

    Cast and crew split even within one observation: `casting` is an existing type with its
    own meaning on the feed, and one body naming the director and the third-billed performer
    is neither beat.
    """
    groups: dict[tuple[UUID, datetime, str], list[CreditAttached]] = {}
    for attached in attachments:
        event_type = CREDIT_ROLE_EVENT_TYPES[attached.role]
        groups.setdefault((attached.film_id, attached.changed_at, event_type), []).append(
            CreditAttached(role=attached.role, name=attached.name)
        )
    return [
        CreditGroup(
            film_id=film_id, event_type=event_type, changed_at=changed_at, credits=tuple(credits)
        )
        for (film_id, changed_at, event_type), credits in groups.items()
    ]


async def load_attachment_backlog(
    session: AsyncSession, *, since: datetime
) -> list[AttachedCredit]:
    """Every seed-grade credit *attachment* recorded at or after `since`, oldest first.

    Detachments are read past. A credit leaving a film is real history — it is what makes a
    later re-attachment a change again — but "X is no longer attached" is not a beat, and a
    card announcing one would mostly report TMDB reverting its own vandalism.

    A fixed rolling window rather than a watermark, for the reason the field-change phase
    documents: a watermark would advance past attachments a *failed* sweep never carded,
    losing them permanently, and the re-read is free because a carded group is skipped.
    """
    stmt = (
        select(
            FilmCreditChange.film_id,
            FilmCreditChange.person_id,
            FilmCreditChange.credit_type,
            FilmCreditChange.job,
            FilmCreditChange.changed_at,
            Person.name,
        )
        .join(Person, Person.id == FilmCreditChange.person_id)
        .where(FilmCreditChange.change == CREDIT_ADDED, FilmCreditChange.changed_at >= since)
        .order_by(FilmCreditChange.changed_at, FilmCreditChange.id)
    )
    attached: list[AttachedCredit] = []
    for row in await session.execute(stmt):
        role = credit_role(row.credit_type, row.job)
        if role is None:
            continue
        attached.append(
            AttachedCredit(
                film_id=row.film_id,
                person_id=row.person_id,
                name=row.name,
                role=role,
                changed_at=row.changed_at,
            )
        )
    return attached


async def _present_seed_roles(
    session: AsyncSession, *, film_ids: set[UUID]
) -> set[tuple[UUID, int, str]]:
    """Every `(film, person, seed-grade role)` that `catalog.film_credit` holds *right now*
    for these films. One query for the whole backlog rather than one per attachment: the
    quarantine gate asks this of every aged row, and the rolling window makes that the same
    rows on every pass for as long as the hold lasts.

    Seed grade is re-derived here rather than assumed from the change row that recorded the
    attachment. `film_credit` is delete-and-rebuilt on every ingest, and a cast member who has
    since slipped out of the top-5 billing no longer holds a seed-grade credit — which is
    exactly how `credit_history` would diff them, as removed. Reading the same predicate is
    what stops the two disagreeing about what "still attached" means.
    """
    if not film_ids:
        return set()
    stmt = select(
        FilmCredit.film_id,
        FilmCredit.person_id,
        FilmCredit.credit_type,
        FilmCredit.job,
        FilmCredit.credit_order,
    ).where(FilmCredit.film_id.in_(film_ids))
    present: set[tuple[UUID, int, str]] = set()
    for row in await session.execute(stmt):
        if not is_seed_grade(row.credit_type, row.job, row.credit_order):
            continue
        role = credit_role(row.credit_type, row.job)
        if role is not None:
            present.add((row.film_id, row.person_id, role))
    return present


async def quarantine_attachments(
    session: AsyncSession,
    *,
    attachments: list[AttachedCredit],
    now: datetime,
    quarantine_hours: int,
) -> tuple[list[AttachedCredit], list[AttachedCredit]]:
    """Split the backlog into the attachments eligible to card and the ones still held
    (ADR-0017, D-3). Returns `(eligible, held)`.

    Two conditions, both required. An attachment is eligible only once
    `changed_at + quarantine_hours <= now` — the window is fully observed — **and** the credit
    is still in `catalog.film_credit` under the same seed-grade role. The second is what makes
    this a quarantine rather than a delay: an edit reverted inside the window is not a beat
    that happened late, it is a beat that never happened, and it must publish nothing at all.

    Both conditions are re-evaluated from scratch on every pass, because both are properties
    of *now* rather than of the row. No `pending` state is written anywhere: the rolling
    window is the queue, which is why the hold must stay inside it
    (`validate_sweep_configuration`).

    This is the attachment-side generalisation of NEU-1205's forward-dwell gate, and the two
    are deliberately not one function. The removal gate asks whether the person came *back*
    within the window, reading raw history because a flap's re-attachment is never carded;
    this one asks whether they are *still here*, reading live state. Same shape, opposite
    sources.

    `0` disables both conditions together, reverting to immediate carding — the setting is one
    switch, and holding a credit for no time while still requiring it to be present would be a
    third behaviour nobody asked for.

    A reverted attachment counts as held rather than as its own outcome. It will be re-read
    and re-held on every pass until it falls out of the rolling window, which is the correct
    end state — there is no card to write and nothing to remember.
    """
    if quarantine_hours <= 0 or not attachments:
        return attachments, []
    hold = timedelta(hours=quarantine_hours)
    aged: list[AttachedCredit] = []
    held: list[AttachedCredit] = []
    for attached in attachments:
        # `<=`, so an attachment lands the pass it turns eligible rather than the one after.
        (aged if attached.changed_at + hold <= now else held).append(attached)
    if not aged:
        return [], held
    present = await _present_seed_roles(session, film_ids={a.film_id for a in aged})
    eligible: list[AttachedCredit] = []
    for attached in aged:
        if (attached.film_id, attached.person_id, attached.role) in present:
            eligible.append(attached)
        else:
            held.append(attached)
    return eligible, held


async def _already_carded(
    session: AsyncSession, *, film_id: UUID, event_type: str, changed_at: datetime
) -> bool:
    """Whether this exact observation already has its card — the fast path under
    `uq_event_catalog_change`, whose triple this is. Cheap enough to run per group, and it
    keeps the rolling window's re-reads off the failure counters."""
    carded = exists().where(
        Event.film_id == film_id,
        Event.event_type == event_type,
        Event.provenance == "catalog",
        Event.occurred_at == changed_at,
    )
    return bool((await session.execute(select(carded))).scalar())


async def _latest_credit_event_types(
    session: AsyncSession, *, film_id: UUID, event_type: str
) -> dict[str, str]:
    """Per-person latest event type among `(event_type, 'credit_removed')`, keyed by
    normalized name. One query, no per-person roundtrips."""
    types = (event_type, CREDIT_REMOVED_EVENT_TYPE)
    stmt = (
        select(Event.subject_key, Event.event_type, Event.occurred_at, Event.created_at)
        .where(
            Event.film_id == film_id,
            Event.event_type.in_(types),
            Event.subject_key.isnot(None),
        )
        .order_by(Event.occurred_at.desc(), Event.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    latest: dict[str, str] = {}
    for subject_key, ev_type, _occurred_at, _created_at in rows:
        for name in subject_key or []:
            latest.setdefault(name, ev_type)
    return latest


async def _uncarded_credits(
    session: AsyncSession, *, film_id: UUID, event_type: str, credits: tuple[CreditAttached, ...]
) -> tuple[CreditAttached, ...]:
    """The credits in a group that should still be carded for this beat.

    Removal-aware (NEU-1200): for each person, look up the most recent event among
    their own attachment type and `credit_removed`. Suppress only if it's an attachment —
    a removal or no prior card means the re-attachment is news.

    Invariant: *the latest card for a person reflects their current attachment state.*

    Scoped to the group's **own** event type: an actor-director is carded once for
    joining the cast and again when TMDB records them as directing. Cross-type
    suppression is unchanged.

    A person is only ever suppressed *individually*: three cast arriving where one was
    already carded still cards the other two.
    """
    latest_types = await _latest_credit_event_types(session, film_id=film_id, event_type=event_type)
    kept: list[CreditAttached] = []
    for c in credits:
        latest = latest_types.get(normalize_name(c.name))
        if latest is None or latest == CREDIT_REMOVED_EVENT_TYPE:
            kept.append(c)
    return tuple(kept)


async def _card_group(session: AsyncSession, *, group: CreditGroup) -> bool:
    """Create the event and its deterministic summary for one group, or report that it was
    already carded. One transaction covers both writes, so an event can never reach the feed
    without the summary row every read path inner-joins. Caller owns the commit."""
    if await _already_carded(
        session, film_id=group.film_id, event_type=group.event_type, changed_at=group.changed_at
    ):
        return False
    credits = await _uncarded_credits(
        session, film_id=group.film_id, event_type=group.event_type, credits=group.credits
    )
    if not credits:
        return False
    event = Event(
        film_id=group.film_id,
        event_type=group.event_type,
        confidence="rumored",
        provenance="catalog",
        occurred_at=group.changed_at,
        region=None,
        subject_key=[normalize_name(c.name) for c in credits],
    )
    session.add(event)
    await session.flush()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=CreditsAttached(credits=credits),
        source_updated_at=event.updated_at,
    )
    return True


async def run_credit_attachment_events(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    now: datetime,
    lookback_days: int,
    quarantine_hours: int = 0,
    failure_threshold: int = 10,
) -> CreditEventResult:
    """Card every seed-grade credit attachment TMDB recorded in the window, less the ones
    quarantine is still holding.

    `quarantine_hours` defaults to 0 — no hold — rather than to the setting's 72, the same way
    the detachment phase's `dwell_days` does: the caller that has a `Settings` passes the
    tuned value, and every other caller gets the pre-D-3 behaviour it was written against.
    """
    result = CreditEventResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)
    since = now - timedelta(days=lookback_days)

    async with owned_session(session_factory) as s:
        backlog = await load_attachment_backlog(s, since=since)
        # Same session as the load: the quarantine gate reads live `film_credit` state
        # against the backlog it just read, and a second session could straddle a refresh
        # that rebuilt those rows mid-check.
        attachments, held = await quarantine_attachments(
            s, attachments=backlog, now=now, quarantine_hours=quarantine_hours
        )
    # The whole backlog, not what survived the gate: "read 40, carded 2, held 37" is the
    # shape that says the phase is working, and netting the held rows out of the read count
    # would make a quarantine that holds everything look like an empty window.
    result.attachments_read = len(backlog)
    result.held = len(held)
    groups = group_attachments(attachments)
    log.info(
        "credit events: %d attachments in %d groups since %s (%d held, quarantine %dh)",
        result.attachments_read,
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
            # so reaching it means a concurrent writer got there first — the group *is*
            # carded. Counting that as a failure would fail the run over the guarantee
            # working, and feed the abort guard on a healthy catalog.
            log.info("credit group %s/%s was carded concurrently", group.film_id, group.event_type)
            result.skipped += 1
            guard.succeeded()
            continue
        except Exception:
            # One unwritable event must not cost the rest of the backlog.
            log.exception("carding credits for film %s failed", group.film_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
                log.error("credit events: %s", result.abort_error)
                return result
            continue
        guard.succeeded()
        if created:
            result.events_created += 1
        else:
            result.skipped += 1

    log.info(
        "credit events: %d created, %d already carded, %d held, %d failed",
        result.events_created,
        result.skipped,
        result.held,
        result.failures,
    )
    return result


# ── Detachment carding phase (NEU-1200) ─────────────────────────────────────


async def load_detachment_backlog(
    session: AsyncSession, *, since: datetime | None = None
) -> list[DetachedCredit]:
    """Every seed-grade credit *detachment* recorded at or after `since`, oldest first.

    When `since` is None (the backfill), reads all history.
    """
    where = FilmCreditChange.change == CREDIT_REMOVED
    if since is not None:
        where &= FilmCreditChange.changed_at >= since

    stmt = (
        select(
            FilmCreditChange.film_id,
            FilmCreditChange.person_id,
            FilmCreditChange.credit_type,
            FilmCreditChange.job,
            FilmCreditChange.changed_at,
            Person.name,
        )
        .join(Person, Person.id == FilmCreditChange.person_id)
        .where(where)
        .order_by(FilmCreditChange.changed_at, FilmCreditChange.id)
    )
    detached: list[DetachedCredit] = []
    for row in await session.execute(stmt):
        role = credit_role(row.credit_type, row.job)
        if role is None:
            continue
        detached.append(
            DetachedCredit(
                film_id=row.film_id,
                person_id=row.person_id,
                name=row.name,
                role=role,
                changed_at=row.changed_at,
            )
        )
    return detached


def group_detachments(detachments: list[DetachedCredit]) -> list[DetachmentGroup]:
    """One group — and so one event — per (film, observation). Pure.

    All roles share one group because `credit_removed` is a single event type and
    `uq_event_catalog_change` allows one catalog event per film, type and timestamp.
    """
    groups: dict[tuple[UUID, datetime], list[DetachedCredit]] = {}
    for detached in detachments:
        groups.setdefault((detached.film_id, detached.changed_at), []).append(detached)
    return [
        DetachmentGroup(film_id=film_id, changed_at=changed_at, credits=tuple(credits))
        for (film_id, changed_at), credits in groups.items()
    ]


async def _has_prior_attachment_card(
    session: AsyncSession, *, film_id: UUID, person_name: str, before: datetime
) -> bool:
    """Whether a visible attachment card exists for this person before `before`."""
    norm = normalize_name(person_name)
    carded = exists().where(
        Event.film_id == film_id,
        Event.event_type.in_(CREDIT_EVENT_TYPES),
        Event.subject_key.any(norm),  # pyright: ignore[reportArgumentType]
        Event.occurred_at < before,
    )
    return bool((await session.execute(select(carded))).scalar())


async def supersede_prior_attachment_cards(session: AsyncSession, *, removal: Event) -> int:
    """Mark the attachment card each person named on `removal` was current on (D-2).

    Per name on the removal's `subject_key`: the most recent *published* attachment card
    (`crew_attached`/`casting`, any provenance) that occurred before the removal is set
    `superseded` with `superseded_by` pointing at the removal. Only the most recent one — an
    older card the same person is on (a trade-story casting card before the catalog carded
    them, say) was already the earlier claim, not the one this removal corrects. Nothing is
    hidden or deleted; the card keeps its place on every surface.

    The card, not the name, is the unit of supersession: `status` lives on the event row, so
    a casting card naming three people is marked when any one of them departs.

    Returns the number of cards marked. Caller owns the commit; `removal` must be flushed so
    its id exists for the FK.
    """
    # Resolve every target before marking any. Marking inside the loop would autoflush the
    # first UPDATE ahead of the next name's query, and a card two departing people share
    # would then fail the `published` filter for the second — handing back an *older* card
    # that person is on, which is not the one this removal corrects.
    targets: dict[UUID, Event] = {}
    for name in removal.subject_key or []:
        stmt = (
            select(Event)
            .where(
                Event.film_id == removal.film_id,
                Event.event_type.in_(CREDIT_EVENT_TYPES),
                Event.subject_key.any(name),  # pyright: ignore[reportArgumentType]
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


async def _has_forward_reattachment(
    session: AsyncSession,
    *,
    film_id: UUID,
    person_id: int,
    role: str,
    after: datetime,
    until: datetime,
) -> bool:
    """Whether the person re-attached to the same seed-grade role in `[after, until)`.

    Reads raw `catalog.film_credit_change` (not `news.event`), because a flap's
    re-attachment is suppressed by removal-aware suppression and is never carded.
    Role-scoped: a cast departure followed by a director arrival is two real events.
    """
    stmt = select(FilmCreditChange.credit_type, FilmCreditChange.job).where(
        FilmCreditChange.film_id == film_id,
        FilmCreditChange.person_id == person_id,
        FilmCreditChange.change == CREDIT_ADDED,
        FilmCreditChange.changed_at >= after,
        FilmCreditChange.changed_at < until,
    )
    rows = await session.execute(stmt)
    for row in rows:
        if credit_role(row.credit_type, row.job) == role:
            return True
    return False


async def _card_detachment_group(
    session: AsyncSession,
    *,
    group: DetachmentGroup,
    now: datetime,
    dwell_days: int,
) -> bool:
    """Create the event and its deterministic summary for one detachment group, or report that
    it was already carded. One transaction covers both writes and the supersession marks on
    the attachment cards it corrects, so a removal can never reach the feed with its
    original still reading `published`. Caller owns the commit."""
    if await _already_carded(
        session,
        film_id=group.film_id,
        event_type=CREDIT_REMOVED_EVENT_TYPE,
        changed_at=group.changed_at,
    ):
        return False

    # Hold: a removal is not eligible to card until the forward window is fully observed.
    eligible_at = group.changed_at + timedelta(days=dwell_days)
    if dwell_days > 0 and eligible_at > now:
        return False

    # Gate 1: keep only people with a prior visible attachment card before this detachment.
    prior_attached: list[DetachedCredit] = []
    for c in group.credits:
        if await _has_prior_attachment_card(
            session, film_id=group.film_id, person_name=c.name, before=group.changed_at
        ):
            prior_attached.append(c)

    # Gate 2: drop flaps — people who re-attached in the same role within the forward window.
    if dwell_days > 0 and prior_attached:
        final_departures: list[DetachedCredit] = []
        for c in prior_attached:
            reattached = await _has_forward_reattachment(
                session,
                film_id=group.film_id,
                person_id=c.person_id,
                role=c.role,
                after=group.changed_at,
                until=eligible_at,
            )
            if not reattached:
                final_departures.append(c)
        prior_attached = final_departures

    if not prior_attached:
        return False

    event = Event(
        film_id=group.film_id,
        event_type=CREDIT_REMOVED_EVENT_TYPE,
        confidence="rumored",
        provenance="catalog",
        occurred_at=group.changed_at,
        region=None,
        subject_key=[normalize_name(c.name) for c in prior_attached],
    )
    session.add(event)
    await session.flush()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=CreditsDetached(
            credits=tuple(CreditDetached(role=c.role, name=c.name) for c in prior_attached)
        ),
        source_updated_at=event.updated_at,
    )
    await supersede_prior_attachment_cards(session, removal=event)
    return True


async def run_credit_detachment_events(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    now: datetime,
    lookback_days: int,
    dwell_days: int = 0,
    failure_threshold: int = 10,
) -> CreditDetachmentResult:
    """Card every seed-grade credit detachment TMDB recorded in the window."""
    result = CreditDetachmentResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)
    since = now - timedelta(days=lookback_days)

    async with owned_session(session_factory) as s:
        detachments = await load_detachment_backlog(s, since=since)
    result.detachments_read = len(detachments)
    groups = group_detachments(detachments)
    log.info(
        "credit detachments: %d detachments in %d groups since %s",
        result.detachments_read,
        len(groups),
        since.isoformat(),
    )

    for group in groups:
        await heartbeat.tick()
        try:
            async with owned_session(session_factory) as s:
                created = await _card_detachment_group(
                    s, group=group, now=now, dwell_days=dwell_days
                )
                if created:
                    await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except IntegrityError:
            log.info("detachment group %s was carded concurrently", group.film_id)
            result.skipped += 1
            guard.succeeded()
            continue
        except Exception:
            log.exception("carding detachments for film %s failed", group.film_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
                log.error("credit detachments: %s", result.abort_error)
                return result
            continue
        guard.succeeded()
        if created:
            result.events_created += 1
        else:
            result.skipped += 1

    log.info(
        "credit detachments: %d created, %d already carded, %d failed",
        result.events_created,
        result.skipped,
        result.failures,
    )
    return result
