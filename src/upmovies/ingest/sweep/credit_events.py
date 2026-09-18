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

**One burst is one card** (ADR-0017, D-7). TMDB routinely gains a whole top-billed cast
between two ingests, and quarantine then releases those credits together however many
observations they arrived over. Attachments are therefore grouped per **(film, event type,
sweep pass)**, not per observation: six cast members whose holds expire in the same pass are
one `casting` card naming all six, dated at the latest `changed_at` among them.
`uq_event_catalog_change` — one catalog event per film, type and timestamp — still holds,
because the group carries exactly one timestamp. Detachments are *not* collapsed this way:
they never pass through quarantine, so they keep the per-observation discipline of one
`credit_removed` card per (film, changed_at).

**An attachment is quarantined before it cards** (ADR-0017, D-3). A `change='added'` row is
eligible only once it has survived `SWEEP_CREDIT_QUARANTINE_HOURS` *and* the credit is still
in `catalog.film_credit` under the same seed-grade role. TMDB is community-edited, and the
edit this suppresses is the one that was never true — vandalism reverted within the hour,
a misfiled credit — which under immediate carding published a beat and then needed a
correction card to take it back. Nothing is written while a row is held: the rolling window
*is* the queue, re-read and re-judged on every pass, which is why the hold has to stay inside
it. The live cast list is never held — state mirrors TMDB immediately, only events wait.

**Sanity checks hold what quarantine cannot** (ADR-0017, D-8). Two shapes of defacement
survive a time window: a vandal attaching one person to dozens of films in a day, each
attachment ordinary on its own, and a credit impossible on its face — someone dead for years,
or an infant billed as a lead. Those are judged on the *person*, not the clock, and they hold
rather than discard: a hold that turns out to be real (a prolific documentary producer, a
posthumous release) still publishes once the condition clears or an admin releases it. Unlike
quarantine, these leave a row — `ingest.credit_hold` — because "one of twenty-five" and "died
in 2011" are not re-derivable from the attachment alone, and because a `deceased` hold is
exactly the kind a human has to be able to point at and override. Birth and death dates are
fetched lazily from `/person/{id}`, once, only for people a card is about to name.

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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

import httpx
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import FilmCredit, FilmCreditChange, Person
from upmovies.catalog.seed_grade import crew_role, is_seed_grade
from upmovies.ingest.models import (
    HOLD_BURST,
    HOLD_DECEASED,
    HOLD_IMPLAUSIBLE_AGE,
    RELEASE_CLEARED,
    RELEASE_EXPIRED,
    RELEASE_MANUAL,
    CreditHold,
)
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.credit_history import CREDIT_ADDED, CREDIT_REMOVED
from upmovies.ingest.tmdb.upsert import ensure_person_details
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
    credit_order_key,
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
    credit_order: int | None = None
    """TMDB's billing position for this credit *right now*, stamped by the quarantine gate
    from the live `catalog.film_credit` row it already reads. None for crew, which has no
    billing position, and None whenever the gate is disabled — see `quarantine_attachments`."""


@dataclass(frozen=True)
class CreditGroup:
    """The attachments one sweep pass collapsed for one film and one event type, and the
    single event they card as."""

    film_id: UUID
    event_type: str
    attachments: tuple[AttachedCredit, ...]
    """In canonical order (`credit_order_key`) — strongest role first, then billing order.

    The group holds the source rows rather than the rendered credits because `occurred_at`
    has to be derived from *whichever of them the card ends up naming*: per-person
    suppression runs after grouping, and a card dated by someone it does not name would be
    both wrong on its face and able to consume a timestamp a later genuine burst needs.
    """

    @property
    def changed_at(self) -> datetime:
        """The *latest* `changed_at` in the group, which the event stores as `occurred_at`
        (D-7). The latest rather than the earliest because a burst is news when its last
        member landed, and because a pass that collapses a strictly larger burst then lands
        on a strictly later timestamp — never colliding with the card the smaller burst
        already wrote under `uq_event_catalog_change`."""
        return max(a.changed_at for a in self.attachments)

    @property
    def credits(self) -> tuple[CreditAttached, ...]:
        """The group as the renderer wants it, in the same canonical order."""
        return tuple(as_credit(a) for a in self.attachments)


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
    holds_new: int = 0
    """Attachments a sanity check opened an `ingest.credit_hold` row for this pass (D-8).

    Apart from `held` above, which is quarantine's, because the two answer different
    questions: quarantine's number is dominated by rows that will never card and is not a
    health signal, while every row counted here is a specific claim — a burst, a death, an
    age — that a named person can go and look at."""
    holds_cleared: int = 0
    """Open holds whose condition lifted, released this pass. They card on this same pass."""
    holds_expired: int = 0
    """Open holds whose change aged out of the rolling window before anything released them.
    Nothing cards: the change is past `SWEEP_EVENT_LOOKBACK_DAYS` and is no longer read."""
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


def as_credit(attached: AttachedCredit) -> CreditAttached:
    """The renderer's view of one attachment. `character` is never set: `film_credit_change`
    does not record it."""
    return CreditAttached(
        role=attached.role, name=attached.name, credit_order=attached.credit_order
    )


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
    """One group — and so one event — per (film, event type) in this pass. Pure.

    Burst collapsing (D-7). The caller passes exactly the attachments this pass will card, so
    "the pass" needs no key of its own: everything handed in is by construction one pass's
    worth. `changed_at` is therefore no longer part of the key — credits that landed over
    four days and came off hold together are one beat, and carding them as four is the burst
    the quarantine window created.

    With the gate disabled (`quarantine_hours=0`) "this pass" is the whole rolling window
    rather than a quarantine release, so a first pass over a backlog collapses it into one
    card. That follows from the key being the pass, which is what the M5 contract specifies;
    it is not a second rule. Steady state is unaffected — each later pass carries one new
    observation, and per-person suppression drops the rest.

    The group's `changed_at` is the **latest** in it, which is what the event stores as
    `occurred_at`. That keeps `uq_event_catalog_change` satisfiable (one timestamp per group)
    and keeps the re-read idempotent: the next pass over the same rolling window regroups the
    same rows to the same latest timestamp and finds the card already there.

    Cast and crew split even within one pass: `casting` is an existing type with its own
    meaning on the feed, and one body naming the director and the third-billed performer is
    neither beat.

    Credits are ordered canonically (`credit_order_key`) — strongest role first, then billing
    order within the role — so the body reads and the `subject_key` stores top-billed first
    rather than in whichever order the history diff emitted.
    """
    groups: dict[tuple[UUID, str], list[AttachedCredit]] = {}
    for attached in attachments:
        key = (attached.film_id, CREDIT_ROLE_EVENT_TYPES[attached.role])
        groups.setdefault(key, []).append(attached)
    return [
        CreditGroup(
            film_id=film_id,
            event_type=event_type,
            attachments=tuple(sorted(grouped, key=credit_order_key)),
        )
        for (film_id, event_type), grouped in groups.items()
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

    Attachments under an **open** sanity hold are read past (D-8). They are still in the
    window and are still re-judged every pass — by `reconcile_holds`, which runs before this
    and releases what has stopped applying — but a held row must reach neither the quarantine
    gate nor a group, because being in a group is what cards it. A released row is back in the
    backlog on the pass that released it, which is what makes a cleared burst card the same
    day; an `expired` one is not, and needs no filtering, because its change is older than
    `since`.
    """
    excluded = await _open_hold_keys(session, since=since)
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
        # Filtered here rather than in the query: the hold is keyed on the *role*, which only
        # `credit_role` knows, so a SQL anti-join could only match on (film, person, time) —
        # and would then withhold an actor-director's directing credit because their casting
        # credit is held.
        if (row.film_id, row.person_id, role, row.changed_at) in excluded:
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


async def _present_seed_credits(
    session: AsyncSession, *, film_ids: set[UUID]
) -> dict[tuple[UUID, int, str], int | None]:
    """Every `(film, person, seed-grade role)` that `catalog.film_credit` holds *right now*
    for these films, mapped to that credit's billing order. One query for the whole backlog
    rather than one per attachment: the quarantine gate asks this of every aged row, and the
    rolling window makes that the same rows on every pass for as long as the hold lasts. The
    burst check's `present` count (D-8) reads it too, which is what makes "still attached" mean
    one thing across both gates.

    Seed grade is re-derived here rather than assumed from the change row that recorded the
    attachment. `film_credit` is delete-and-rebuilt on every ingest, and a cast member who has
    since slipped out of the top-5 billing no longer holds a seed-grade credit — which is
    exactly how `credit_history` would diff them, as removed. Reading the same predicate is
    what stops the two disagreeing about what "still attached" means.

    Membership answers the gate; the value answers D-7's body ordering. Both are properties of
    the same live row, so they are read together — `film_credit_change` records no billing
    position of its own, and asking for it in a second query would be asking twice.
    """
    if not film_ids:
        return {}
    stmt = select(
        FilmCredit.film_id,
        FilmCredit.person_id,
        FilmCredit.credit_type,
        FilmCredit.job,
        FilmCredit.credit_order,
    ).where(FilmCredit.film_id.in_(film_ids))
    present: dict[tuple[UUID, int, str], int | None] = {}
    for row in await session.execute(stmt):
        if not is_seed_grade(row.credit_type, row.job, row.credit_order):
            continue
        role = credit_role(row.credit_type, row.job)
        if role is not None:
            present[(row.film_id, row.person_id, role)] = row.credit_order
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

    Every eligible attachment comes back stamped with its live `credit_order`, which D-7's
    body ordering needs and only `film_credit` has. Disabling the gate disables the stamp
    with it: `0` reads no live state at all, by design, so a cast body written under it falls
    back to the order the history diff produced. That is the pre-D-3 behaviour the `0` path
    exists to preserve, not a second ordering rule.
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
    present = await _present_seed_credits(session, film_ids={a.film_id for a in aged})
    eligible: list[AttachedCredit] = []
    for attached in aged:
        key = (attached.film_id, attached.person_id, attached.role)
        if key in present:
            eligible.append(replace(attached, credit_order=present[key]))
        else:
            held.append(attached)
    return eligible, held


# ── Sanity holds (ADR-0017, D-8) ─────────────────────────────────────────────


@dataclass
class SanityHoldCounts:
    """What one pass did to `ingest.credit_hold`: rows it opened, rows whose condition lifted,
    and rows whose change aged out of the rolling window before either happened."""

    new: int = 0
    cleared: int = 0
    expired: int = 0


def observation_day(changed_at: datetime) -> date:
    """The UTC day an attachment was observed on — the burst check's bucket.

    UTC rather than any local day, because `changed_at` is the time *we* recorded the diff at
    and TMDB's editors are everywhere; a local boundary would split one editor's evening
    across two buckets and merge two others' into one.
    """
    return changed_at.astimezone(UTC).date()


def years_before(day: date, years: int) -> date:
    """`day` moved back a whole number of years, 29 February landing on the 28th.

    Whole years rather than `365 * years` days because both checks are stated in years and are
    read by humans against birthdays: "under 3 at the time" has to mean the same thing as it
    does on a passport, leap days included.
    """
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


@dataclass(frozen=True)
class BurstCount:
    """How many distinct films one `(person, UTC day)` reached, counted the two ways the burst
    rule needs.

    `recorded` decides whether to **hold**: the person's same-day seed-grade `added` rows,
    whatever became of them since. A vandalism run is what it was even after TMDB has reverted
    part of it, and detecting on live state would let a partial revert walk the survivors
    straight past the check.

    `present` decides whether to **release**: the subset `catalog.film_credit` still backs,
    which is the same live-state read the quarantine gate makes. It is the only one of the two
    that can fall, because history is append-only — so it is the only one that can ever lift a
    hold.

    The two disagree exactly when TMDB has reverted part of a burst, which is why a released
    hold is never re-held: `recorded` would hold it again on the next pass forever.
    """

    recorded: int
    present: int


async def _burst_counts(
    session: AsyncSession, *, person_days: set[tuple[int, date]]
) -> dict[tuple[int, date], BurstCount]:
    """The recorded and still-present film counts for each `(person, UTC day)`. Two queries for
    the whole set, whichever count the caller is about to read."""
    if not person_days:
        return {}
    days = {day for _, day in person_days}
    lower = datetime.combine(min(days), time.min, tzinfo=UTC)
    upper = datetime.combine(max(days) + timedelta(days=1), time.min, tzinfo=UTC)
    stmt = select(
        FilmCreditChange.film_id,
        FilmCreditChange.person_id,
        FilmCreditChange.credit_type,
        FilmCreditChange.job,
        FilmCreditChange.changed_at,
    ).where(
        FilmCreditChange.change == CREDIT_ADDED,
        FilmCreditChange.person_id.in_({person_id for person_id, _ in person_days}),
        FilmCreditChange.changed_at >= lower,
        FilmCreditChange.changed_at < upper,
    )
    candidates: dict[tuple[int, date], set[tuple[UUID, str]]] = {}
    film_ids: set[UUID] = set()
    for row in await session.execute(stmt):
        role = credit_role(row.credit_type, row.job)
        if role is None:
            continue
        key = (row.person_id, observation_day(row.changed_at))
        # The date range above is a bounding box over every day asked about, so it also
        # returns the days between them — which belong to nobody's question.
        if key not in person_days:
            continue
        candidates.setdefault(key, set()).add((row.film_id, role))
        film_ids.add(row.film_id)
    present = await _present_seed_credits(session, film_ids=film_ids)
    return {
        key: BurstCount(
            recorded=len({film_id for film_id, _role in pairs}),
            present=len({film_id for film_id, role in pairs if (film_id, key[0], role) in present}),
        )
        for key, pairs in candidates.items()
    }


async def _open_hold_keys(
    session: AsyncSession, *, since: datetime
) -> set[tuple[UUID, int, str, datetime]]:
    """Every attachment an open hold is currently withholding, as the backlog's own key."""
    stmt = select(
        CreditHold.film_id, CreditHold.person_id, CreditHold.credit_type, CreditHold.changed_at
    ).where(CreditHold.released_at.is_(None), CreditHold.changed_at >= since)
    return {
        (film_id, person_id, role, changed_at)
        for film_id, person_id, role, changed_at in await session.execute(stmt)
    }


async def _released_keys(
    session: AsyncSession, *, attachments: list[AttachedCredit]
) -> set[tuple[UUID, int, str, datetime]]:
    """The attachments a hold has already let go of, which no check may hold again.

    A release has to be final for the observation it names, or it lasts exactly one pass. Both
    reasons that re-admit a change need this, for the same reason and against different rules:

    - `manual` — an admin overrode a `deceased` hold on Monday; the person is still dead on
      Tuesday, and without this the check re-holds it every pass forever.
    - `cleared` — a burst whose live count has fallen below the threshold. Detection reads the
      *recorded* count, which is append-only and therefore still above it, so re-holding is
      exactly what would happen. This is the seam between the two counts in `BurstCount`.

    `expired` is absent deliberately: an expired hold's change is older than the lookback and
    never reaches a backlog again, so it needs no immunity and granting it one would keep a
    row's decision alive past the window that justified it.
    """
    if not attachments:
        return set()
    stmt = select(
        CreditHold.film_id, CreditHold.person_id, CreditHold.credit_type, CreditHold.changed_at
    ).where(
        CreditHold.release_reason.in_((RELEASE_MANUAL, RELEASE_CLEARED)),
        CreditHold.film_id.in_({a.film_id for a in attachments}),
    )
    return {
        (film_id, person_id, role, changed_at)
        for film_id, person_id, role, changed_at in await session.execute(stmt)
    }


def _hold_key(attached: AttachedCredit) -> tuple[UUID, int, str, datetime]:
    """`uq_credit_hold_change` as the phase holds it — the role, not TMDB's cast/crew split."""
    return (attached.film_id, attached.person_id, attached.role, attached.changed_at)


async def reconcile_holds(
    session: AsyncSession, *, now: datetime, since: datetime, max_films_per_day: int
) -> SanityHoldCounts:
    """Release every open hold that has stopped applying, before the backlog is read. Caller
    commits.

    Two endings, and they are not symmetric:

    - **cleared** — the condition lifted. Only `burst` can: it is a claim about a *set* of
      rows, and TMDB reverting most of a vandalism run makes it false. Re-running
      `_burst_counts` is the whole test, so detection and release cannot drift apart. The
      survivors card on this same pass, which is why this runs before the backlog is loaded.
    - **expired** — the change fell out of `SWEEP_EVENT_LOOKBACK_DAYS`. Nothing reads it any
      more, so nothing will card it; the row is closed to say the hold ended rather than left
      open forever to say nothing. Applies to every reason, including the two that never
      clear: `deceased` and `implausible_age` are claims about the person, and a person does
      not stop having died.

    Clearing reads `BurstCount.present` while detection reads `BurstCount.recorded`, which is
    the only way both halves of §3 can hold at once: history is append-only, so a rule that
    detected on the same count it releases on could never release anything. `_released_keys`
    is the other half of that seam — a cleared row is never re-held.

    A `max_films_per_day` of 0 turns the burst check off, and turning a check off releases
    what it is holding outright: the condition it stated can no longer be evaluated, so
    continuing to withhold on it would be a hold nothing can ever lift.
    """
    counts = SanityHoldCounts()
    open_holds = (
        (await session.execute(select(CreditHold).where(CreditHold.released_at.is_(None))))
        .scalars()
        .all()
    )
    live: list[CreditHold] = []
    for hold in open_holds:
        if hold.changed_at < since:
            _release(hold, now=now, reason=RELEASE_EXPIRED)
            counts.expired += 1
        else:
            live.append(hold)

    bursts = [hold for hold in live if hold.reason == HOLD_BURST]
    if bursts:
        by_day = (
            await _burst_counts(
                session,
                person_days={(h.person_id, observation_day(h.changed_at)) for h in bursts},
            )
            if max_films_per_day > 0
            else {}
        )
        for hold in bursts:
            key = (hold.person_id, observation_day(hold.changed_at))
            count = by_day.get(key)
            # `is None` covers both "the check is off" and "this person has no same-day rows
            # left at all", which are the same answer: nothing is holding this any more.
            if max_films_per_day <= 0 or count is None or count.present < max_films_per_day:
                _release(hold, now=now, reason=RELEASE_CLEARED)
                counts.cleared += 1
    await session.flush()
    return counts


def _release(hold: CreditHold, *, now: datetime, reason: str) -> None:
    hold.released_at = now
    hold.release_reason = reason
    log.info(
        "credit hold released: film %s person %s (%s), held as %s, released %s",
        hold.film_id,
        hold.person_id,
        hold.credit_type,
        hold.reason,
        reason,
    )


def _date_hold(
    person: Person, *, changed_on: date, posthumous_years: int, min_age_years: int
) -> str | None:
    """The reason this person's dates disqualify a credit observed on `changed_on`, or None.

    Both tests are one-sided on purpose: a NULL date never holds anything. `catalog.person`
    cannot tell "no death recorded" from "alive", and most people have no birthday there at
    all, so reading an absent date as evidence would hold the credits of everyone TMDB is
    simply thin on.

    A posthumous credit inside `posthumous_years` is ordinary — a film completed before the
    death, archive footage, a voice recorded years earlier — so the check is for credits that
    arrive long after, which is the shape vandalism and misfiles take.
    """
    if (
        posthumous_years > 0
        and person.deathday is not None
        and person.deathday < years_before(changed_on, posthumous_years)
    ):
        return HOLD_DECEASED
    if (
        min_age_years > 0
        and person.birthday is not None
        and person.birthday > years_before(changed_on, min_age_years)
    ):
        return HOLD_IMPLAUSIBLE_AGE
    return None


async def _person_dates(session: AsyncSession, client: TMDBClient, person_id: int) -> Person | None:
    """`ensure_person_details`, with a TMDB outage demoted to "no dates known".

    The date checks are a *filter over what would otherwise card*, so failing to reach TMDB
    must cost the pass nothing more than the holds it would have placed. Raising here would
    cost the whole credits phase — this runs before the per-group loop that has the failure
    guard — over an endpoint that decides nothing for the overwhelming majority of people.
    Nothing is stamped on a failure, so the next pass asks again.
    """
    try:
        return await ensure_person_details(session, client, person_id)
    except httpx.HTTPError:
        log.warning("person %s details unavailable; sanity dates not checked", person_id)
        return None


async def _write_holds(
    session: AsyncSession, *, holds: list[tuple[AttachedCredit, str]], now: datetime
) -> int:
    """Open (or re-open) one hold row per held attachment. Caller commits.

    Re-opening rather than inserting beside: the grain is the observation, so a change that
    was held, cleared, and then tripped a check again is one row that has been held twice, not
    two rows. Manually released rows never reach here — `_manual_release_keys` filters them
    out upstream, which is where the override has to be honoured anyway so the attachment
    stays eligible to card.
    """
    if not holds:
        return 0
    rows: dict[tuple[UUID, int, str, datetime], dict] = {}
    for attached, reason in holds:
        rows.setdefault(
            _hold_key(attached),
            {
                "film_id": attached.film_id,
                "person_id": attached.person_id,
                "credit_type": attached.role,
                "changed_at": attached.changed_at,
                "reason": reason,
                "held_at": now,
            },
        )
        log.info(
            "credit hold: film %s person %s (%s) held as %s",
            attached.film_id,
            attached.person_id,
            attached.role,
            reason,
        )
    stmt = insert(CreditHold).values(list(rows.values()))
    stmt = stmt.on_conflict_do_update(
        constraint="uq_credit_hold_change",
        set_={
            "reason": stmt.excluded.reason,
            "held_at": stmt.excluded.held_at,
            "released_at": None,
            "release_reason": None,
        },
    )
    await session.execute(stmt)
    return len(rows)


async def sanity_holds(
    session: AsyncSession,
    *,
    client: TMDBClient | None,
    attachments: list[AttachedCredit],
    now: datetime,
    max_films_per_day: int,
    posthumous_years: int,
    min_age_years: int,
) -> tuple[list[AttachedCredit], SanityHoldCounts]:
    """Split the quarantine's survivors into the ones that may card and the ones a sanity
    check withholds (D-8). Returns `(eligible, counts)`. Caller commits.

    Three checks, in this order and for a cost reason: **burst** is a claim about rows we
    already hold and is answered in two queries for the whole backlog, while **deceased** and
    **implausible age** each need `/person/{id}` for a person we have never fetched. Running
    burst first means a twenty-five-film vandalism run costs no TMDB requests at all — the
    people in it are no longer about to be carded, which is the only condition under which
    the spec permits the fetch.

    Where quarantine asks *did this edit survive*, these ask *is this edit possible*. Both
    hold rather than discard, but only these leave a row behind: quarantine's two conditions
    are re-derivable from the rolling window on every pass, while "one of twenty-five" and
    "died in 2011" are not answerable from the attachment alone.

    Each check is disabled by a threshold of 0, and a `client` of None disables the two that
    need TMDB — the same shape as `quarantine_hours=0` above, so a caller without a `Settings`
    gets the pre-D-8 behaviour it was written against.

    **What "about to be carded" means here.** The spec places these after the quarantine gate
    and before `_card_group`, and that is where they are — which means a person whose group
    `_card_group` will then find already carded is fetched too, because per-person suppression
    and the already-carded check both need the group and run inside it. What bounds the cost is
    `details_observed_at`, not the placement: a person is fetched once ever, so this is a
    one-off at deploy over whatever is in the first rolling window, not a per-pass charge.
    """
    counts = SanityHoldCounts()
    if not attachments:
        return [], counts
    immune = await _released_keys(session, attachments=attachments)
    holds: list[tuple[AttachedCredit, str]] = []

    survivors: list[AttachedCredit] = []
    if max_films_per_day > 0:
        by_day = await _burst_counts(
            session,
            person_days={(a.person_id, observation_day(a.changed_at)) for a in attachments},
        )
        for attached in attachments:
            count = by_day.get((attached.person_id, observation_day(attached.changed_at)))
            recorded = count.recorded if count is not None else 0
            if recorded >= max_films_per_day and _hold_key(attached) not in immune:
                holds.append((attached, HOLD_BURST))
            else:
                survivors.append(attached)
    else:
        survivors = list(attachments)

    eligible: list[AttachedCredit] = []
    if client is not None and (posthumous_years > 0 or min_age_years > 0):
        people: dict[int, Person | None] = {}
        for attached in survivors:
            if attached.person_id not in people:
                people[attached.person_id] = await _person_dates(
                    session, client, attached.person_id
                )
            person = people[attached.person_id]
            reason = (
                None
                if person is None
                else _date_hold(
                    person,
                    changed_on=observation_day(attached.changed_at),
                    posthumous_years=posthumous_years,
                    min_age_years=min_age_years,
                )
            )
            if reason is not None and _hold_key(attached) not in immune:
                holds.append((attached, reason))
            else:
                eligible.append(attached)
    else:
        eligible = survivors

    counts.new = await _write_holds(session, holds=holds, now=now)
    return eligible, counts


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


async def _uncarded_attachments(
    session: AsyncSession,
    *,
    film_id: UUID,
    event_type: str,
    attachments: tuple[AttachedCredit, ...],
) -> tuple[AttachedCredit, ...]:
    """The attachments in a group that should still be carded for this beat.

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
    kept: list[AttachedCredit] = []
    for a in attachments:
        latest = latest_types.get(normalize_name(a.name))
        if latest is None or latest == CREDIT_REMOVED_EVENT_TYPE:
            kept.append(a)
    return tuple(kept)


async def _card_group(session: AsyncSession, *, group: CreditGroup) -> bool:
    """Create the event and its deterministic summary for one group, or report that it was
    already carded. One transaction covers both writes, so an event can never reach the feed
    without the summary row every read path inner-joins. Caller owns the commit.

    Per-person suppression runs **before** the already-carded check, and the card is dated by
    what survives it. Under burst collapsing the group's latest `changed_at` usually belongs
    to someone the card will not name — they were carded on an earlier pass, or by a trade
    story — and dating the card by the whole group would both misdate it and let it collide
    with a card that already holds that timestamp, silently dropping everyone still owed one.
    """
    attachments = await _uncarded_attachments(
        session,
        film_id=group.film_id,
        event_type=group.event_type,
        attachments=group.attachments,
    )
    if not attachments:
        return False
    occurred_at = max(a.changed_at for a in attachments)
    if await _already_carded(
        session, film_id=group.film_id, event_type=group.event_type, changed_at=occurred_at
    ):
        return False
    credits = tuple(as_credit(a) for a in attachments)
    event = Event(
        film_id=group.film_id,
        event_type=group.event_type,
        confidence="rumored",
        provenance="catalog",
        occurred_at=occurred_at,
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
    client: TMDBClient | None = None,
    max_films_per_day: int = 0,
    posthumous_years: int = 0,
    min_age_years: int = 0,
    failure_threshold: int = 10,
) -> CreditEventResult:
    """Card every seed-grade credit attachment TMDB recorded in the window, less the ones
    quarantine and the sanity checks are still holding.

    Every gate defaults to off — `quarantine_hours` to 0 rather than the setting's 72, the
    three sanity thresholds likewise, and `client` to None — the same way the detachment
    phase's `dwell_days` does: the caller that has a `Settings` passes the tuned values, and
    every other caller gets the behaviour it was written against. `client` is separate from
    the thresholds because the two date checks need a TMDB request and the burst check does
    not, so a caller with no client still gets the whole of D-8 that costs nothing.
    """
    result = CreditEventResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)
    since = now - timedelta(days=lookback_days)

    # Before the backlog is read, so a hold released here is back in it on this pass: a burst
    # TMDB has reverted cards its survivors today rather than tomorrow. Its own session and
    # its own commit, because the releases must stand even if the carding below fails.
    async with owned_session(session_factory) as s:
        reconciled = await reconcile_holds(
            s, now=now, since=since, max_films_per_day=max_films_per_day
        )
        await s.commit()
    result.holds_cleared = reconciled.cleared
    result.holds_expired = reconciled.expired

    async with owned_session(session_factory) as s:
        backlog = await load_attachment_backlog(s, since=since)
        # Same session as the load: the quarantine gate reads live `film_credit` state
        # against the backlog it just read, and a second session could straddle a refresh
        # that rebuilt those rows mid-check. The sanity checks read the same live state, and
        # join the same session for the same reason.
        attachments, held = await quarantine_attachments(
            s, attachments=backlog, now=now, quarantine_hours=quarantine_hours
        )
        attachments, sane = await sanity_holds(
            s,
            client=client,
            attachments=attachments,
            now=now,
            max_films_per_day=max_films_per_day,
            posthumous_years=posthumous_years,
            min_age_years=min_age_years,
        )
        await s.commit()
    # The whole backlog, not what survived the *quarantine* gate: "read 40, carded 2, held 37"
    # is the shape that says the phase is working, and netting those rows out of the read count
    # would make a quarantine that holds everything look like an empty window.
    #
    # Rows under an open sanity hold are the one thing it does not include, because
    # `load_attachment_backlog` never returns them (D-8). That is deliberate rather than an
    # omission: a held row is not a row this pass declined to card, it is one an earlier pass
    # already decided about, and the `holds:` counts are where it is accounted for. Reading a
    # burst as a *shrinking* window is the cost, and it is why those counts are on the detail
    # line beside this one rather than folded into it.
    result.attachments_read = len(backlog)
    result.held = len(held)
    result.holds_new = sane.new
    groups = group_attachments(attachments)
    log.info(
        "credit events: %d attachments in %d groups since %s "
        "(%d held, quarantine %dh; holds %d new, %d cleared, %d expired)",
        result.attachments_read,
        len(groups),
        since.isoformat(),
        result.held,
        quarantine_hours,
        result.holds_new,
        result.holds_cleared,
        result.holds_expired,
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
        "credit events: %d created, %d already carded, %d held, %d newly on hold, %d failed",
        result.events_created,
        result.skipped,
        result.held,
        result.holds_new,
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
