"""The sweep's studio phase: turn production-company attachments and detachments into events
(EF-5, NEU-1433).

`catalog.film_production_company` is delete-and-rebuilt on every ingest, so
`ingest.tmdb.company_history` had to build the history this phase reads — the same debt the
credit half paid in NEU-1082. What it buys is the whole of what a studio follow delivers
(EF-3): a follower of Legendary hears that Legendary joined a film, and nothing else about
that film.

Deliberately built to `ingest.sweep.credit_events`' shape, one phase rather than two, and the
differences from it are all consequences of one fact: **a company attachment and a company
detachment are the same kind of observation read with opposite signs**, where a credit
attachment and a credit detachment are not.

- **Both directions quarantine** (D-3, `SWEEP_CREDIT_QUARANTINE_HOURS`). An `added` row cards
  only once the window is fully observed *and* the company is still on the film; a `removed`
  row only once the window is observed *and* the company is still gone. The credit half needed
  a separate forward-dwell gate for removals (NEU-1205) because its removals never went
  through quarantine at all; here the absence check *is* the flap gate, and one setting covers
  both signs — with one more step, because live state alone cannot see a revert from the far
  side: a reverted attach leaves a `removed` row whose company really is gone, which the gate
  reads as a genuine departure. `mark_window_attachments` pairs the two rows so the round trip
  publishes nothing in either direction.
- **One pass, one card, per (film, event type)** (D-7). A film gaining its studio, its
  financier and its production arm in one TMDB edit is one beat and one body naming all three.
  Both types group the same way, unlike the credit half — where detachments keep a
  per-observation grain because they never pass through a quarantine release that would
  collapse them.
- **A removal card supersedes the attach card it corrects** (D-2), per company, exactly as
  `credit_removed` does per person.
- **Confidence is `rumored`** for the reason a credit attachment is (ADR-0002 makes TMDB the
  record for its own *scalar* fields, and a company row is not one), published only after the
  quarantine window — which is what EF-8 means by a catalog attach card being confirmed by
  construction for the push decision.

**No prior-attach-card gate.** `credit_removed` cards only for people who already have a
visible attachment card (NEU-1200's gate 1). That rule is deliberately *not* mirrored, and the
reason is the baseline rule: every film in the catalog on the day this ships has its companies
recorded as a baseline, so almost no studio on almost any film has an attach card for years to
come. Requiring one would silence essentially every detachment the phase could raise, which is
half of what EF-3 promises a studio follower. A detachment with no attachment card is a
complete beat on its own — "Legendary is no longer attached" needs no prior card to be news —
where a person's is not, because the credit half's cards are the only reason a reader knew
they were attached.

**Sanity hold: a company attached to too many films in one observation day** (D-8's shape, its
own threshold). Stateless — no `ingest.credit_hold` row, and that is a decision rather than an
omission. Two of D-8's three checks have no company analogue at all (a studio has no birthday
and does not die), and the third, the burst, is the one whose *release* condition the credit
half has to keep a row for only because it detects on the append-only `recorded` count and so
could never release on it. Detecting on the live count instead makes release automatic and the
row unnecessary: a burst TMDB has reverted below the threshold cards its survivors on the next
pass by itself. What is lost with the row is the admin-facing escape hatch
(`ingest.credit_holds`), which is why the threshold is set well above what a real studio does
in a day — see `SWEEP_COMPANY_SANITY_MAX_FILMS_PER_DAY`.

Contract with the pipeline conventions, matching the other phases: one session per item so a
failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures, and **no `finalize_run`** — all phases share one `ingest_run` row.
"""

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import FilmCompanyChange, FilmProductionCompany, ProductionCompany
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.company_history import COMPANY_ADDED, COMPANY_REMOVED
from upmovies.news.catalog_events import (
    COMPANY_ATTACHED_EVENT_TYPE,
    COMPANY_EVENT_TYPES,
    COMPANY_REMOVED_EVENT_TYPE,
)
from upmovies.news.models import Event
from upmovies.news.subject_key import company_ids_in, company_subject_token
from upmovies.synthesize.deterministic import (
    CompaniesAttached,
    CompaniesDetached,
    CompanyAttached,
    CompanyDetached,
    write_deterministic_summary,
)

log = logging.getLogger(__name__)

# Which event type each `film_company_change.change` cards as. The dict is the registration:
# a third `change` value would fail to subscript it rather than card as something arbitrary.
COMPANY_CHANGE_EVENT_TYPES: dict[str, str] = {
    COMPANY_ADDED: COMPANY_ATTACHED_EVENT_TYPE,
    COMPANY_REMOVED: COMPANY_REMOVED_EVENT_TYPE,
}


@dataclass(frozen=True)
class CompanyChangeRow:
    """One row of `catalog.film_company_change`, with its company named."""

    film_id: UUID
    company_id: int
    name: str
    change: str
    changed_at: datetime
    attached_in_window: bool = False
    """Whether this film's history holds an earlier `added` row for this company inside the
    rolling window — set by `mark_window_attachments`, meaningful only on a `removed` row.

    It is what tells a *round trip* from a *departure*. A company on a film since its baseline
    has no `added` row at all, so its departure is a complete beat on its own; a company that
    attached inside the window and left again has one, and if that attachment never published
    the departure is a card about something no reader was ever told."""

    @property
    def event_type(self) -> str:
        return COMPANY_CHANGE_EVENT_TYPES[self.change]


@dataclass(frozen=True)
class CompanyGroup:
    """The company changes one sweep pass collapsed for one film and one event type, and the
    single event they card as."""

    film_id: UUID
    event_type: str
    changes: tuple[CompanyChangeRow, ...]
    """Ordered by company name, then id.

    TMDB's payload order — which does carry meaning, the lead studio first — is not recorded:
    `film_production_company` is a two-column join with no ordinal, and adding one to carry it
    through the rebuild is a schema change EF-5 did not ask for. Name order is the honest
    alternative: it is stable across passes, which is what keeps a regrouped card rendering
    identically, and it is not a ranking the data cannot support.
    """

    @property
    def changed_at(self) -> datetime:
        """The *latest* `changed_at` in the group, which the event stores as `occurred_at`
        (D-7) — the credit half's rule and its reasoning in full: a burst is news when its last
        member landed, and a pass collapsing a strictly larger burst lands on a strictly later
        timestamp, so it never collides with the card the smaller burst already wrote under
        `uq_event_catalog_change`."""
        return max(c.changed_at for c in self.changes)


@dataclass
class CompanyEventResult:
    """What one company-events pass read and wrote."""

    changes_read: int = 0
    events_created: int = 0
    skipped: int = 0
    """Groups that were already carded by an earlier pass over the same rolling window. The
    steady state, not a problem."""
    held: int = 0
    """Change rows the quarantine gate withheld this pass (D-3): still inside the window, or
    already reverted and so never true. Read against `changes_read` and **not** as a health
    signal, for the reason `CreditEventResult.held` documents — in steady state the reverted
    rows dominate it, which is the gate working."""
    bursts_held: int = 0
    """Change rows the sanity check withheld this pass: one company reaching too many films in
    one observation day. Apart from `held`, because unlike quarantine's number every row
    counted here is a specific claim about a named company that somebody can go and look at."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


def observation_day(changed_at: datetime) -> date:
    """The UTC day a change was observed on — the burst check's bucket, on
    `credit_events.observation_day`'s reasoning: `changed_at` is the time *we* recorded the
    diff at, and a local boundary would split one editor's session across two buckets."""
    return changed_at.astimezone(UTC).date()


async def load_company_backlog(session: AsyncSession, *, since: datetime) -> list[CompanyChangeRow]:
    """Every company change recorded at or after `since`, oldest first, in both directions.

    A fixed rolling window rather than a watermark, for the reason every carding phase here
    uses one: a watermark would advance past changes a *failed* sweep never carded, losing them
    permanently, and the re-read is free because a carded group is skipped.

    Changes a story already published are read past (`carded_by_event_id`), which is the seam
    EF-12's organisation resolution will arrive on. Nothing stamps the column yet, so today
    this filter is always true — it is here rather than in a later migration because the loader
    is where the short-circuit has to be honoured, and a filter added after the fact is one a
    backfill would have to reason about.
    """
    stmt = (
        select(
            FilmCompanyChange.film_id,
            FilmCompanyChange.company_id,
            FilmCompanyChange.change,
            FilmCompanyChange.changed_at,
            ProductionCompany.name,
        )
        .join(ProductionCompany, ProductionCompany.id == FilmCompanyChange.company_id)
        .where(
            FilmCompanyChange.changed_at >= since,
            FilmCompanyChange.carded_by_event_id.is_(None),
        )
        .order_by(FilmCompanyChange.changed_at, FilmCompanyChange.id)
    )
    return [
        CompanyChangeRow(
            film_id=row.film_id,
            company_id=row.company_id,
            name=row.name,
            change=row.change,
            changed_at=row.changed_at,
        )
        for row in await session.execute(stmt)
    ]


def mark_window_attachments(changes: list[CompanyChangeRow]) -> list[CompanyChangeRow]:
    """Stamp every `removed` row that has an earlier `added` row for the same (film, company)
    in this window. Pure; order preserved.

    Computed over the **whole backlog**, before either gate runs, because the attachment this
    asks about is usually one quarantine is *holding* — that is the entire case it exists for.
    A revert writes two rows, an `added` and a `removed`, and the gate judges each on live
    state alone: the `added` is withheld because the company is gone, and the `removed` sails
    through for the very same reason. Live state cannot tell those two rows apart, so the
    pairing has to be read off the history.
    """
    attached: set[tuple[UUID, int]] = set()
    marked: list[CompanyChangeRow] = []
    for change in sorted(changes, key=lambda c: c.changed_at):
        key = (change.film_id, change.company_id)
        if change.change == COMPANY_ADDED:
            attached.add(key)
            marked.append(change)
        else:
            marked.append(replace(change, attached_in_window=key in attached))
    return marked


async def _present_attachments(
    session: AsyncSession, *, film_ids: set[UUID]
) -> set[tuple[UUID, int]]:
    """The `(film_id, company_id)` pairs `catalog.film_production_company` holds right now, for
    the films asked about. One query for the whole backlog."""
    if not film_ids:
        return set()
    stmt = select(FilmProductionCompany.film_id, FilmProductionCompany.company_id).where(
        FilmProductionCompany.film_id.in_(film_ids)
    )
    return {(film_id, company_id) for film_id, company_id in await session.execute(stmt)}


async def quarantine_company_changes(
    session: AsyncSession,
    *,
    changes: list[CompanyChangeRow],
    now: datetime,
    quarantine_hours: int,
) -> tuple[list[CompanyChangeRow], list[CompanyChangeRow]]:
    """Split the backlog into the changes eligible to card and the ones still held (D-3).
    Returns `(eligible, held)`.

    Two conditions, both required. The window must be fully observed —
    `changed_at + quarantine_hours <= now` — **and** live state must still agree with the
    change: an `added` row's company still on the film, a `removed` row's company still off it.
    The second is what makes this a quarantine rather than a delay, and it is the whole flap
    gate for the removal half: an attach reverted inside the window, or a detach reverted
    inside it, is not a beat that happened late but one that never happened, and it must
    publish nothing at all.

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
    aged: list[CompanyChangeRow] = []
    held: list[CompanyChangeRow] = []
    for change in changes:
        # `<=`, so a change lands the pass it turns eligible rather than the one after.
        (aged if change.changed_at + hold <= now else held).append(change)
    if not aged:
        return [], held
    present = await _present_attachments(session, film_ids={c.film_id for c in aged})
    eligible: list[CompanyChangeRow] = []
    for change in aged:
        attached = (change.film_id, change.company_id) in present
        if attached == (change.change == COMPANY_ADDED):
            eligible.append(change)
        else:
            held.append(change)
    return eligible, held


async def _burst_days(
    session: AsyncSession,
    *,
    company_days: set[tuple[int, date]],
    max_films_per_day: int,
) -> set[tuple[int, date]]:
    """The `(company, UTC day)` pairs whose attachment run is large enough to withhold.

    Counted on **live** state — attachments recorded that day that
    `catalog.film_production_company` still backs — rather than on the append-only history the
    credit half detects on. That is what makes the check releasable without a hold row: a
    defacement run TMDB has reverted below the threshold stops being held by itself, on the
    next pass, where a recorded-count rule could never stop holding it.

    The cost of reading live state is the one the credit half's `BurstCount` names: a partial
    revert lets the survivors through. That is the right trade here, because the survivors of a
    reverted run are, by the same live read, exactly the attachments TMDB still asserts — and
    because nothing else can ever let them out.
    """
    if not company_days or max_films_per_day <= 0:
        return set()
    days = {day for _, day in company_days}
    lower = datetime.combine(min(days), time.min, tzinfo=UTC)
    upper = datetime.combine(max(days) + timedelta(days=1), time.min, tzinfo=UTC)
    stmt = (
        select(
            FilmCompanyChange.company_id,
            FilmCompanyChange.film_id,
            FilmCompanyChange.changed_at,
        )
        .join(
            FilmProductionCompany,
            (FilmProductionCompany.film_id == FilmCompanyChange.film_id)
            & (FilmProductionCompany.company_id == FilmCompanyChange.company_id),
        )
        .where(
            FilmCompanyChange.change == COMPANY_ADDED,
            FilmCompanyChange.company_id.in_({company_id for company_id, _ in company_days}),
            FilmCompanyChange.changed_at >= lower,
            FilmCompanyChange.changed_at < upper,
        )
    )
    films: dict[tuple[int, date], set[UUID]] = {}
    for row in await session.execute(stmt):
        key = (row.company_id, observation_day(row.changed_at))
        # The date range above is a bounding box over every day asked about, so it also returns
        # the days between them — which belong to nobody's question.
        if key not in company_days:
            continue
        films.setdefault(key, set()).add(row.film_id)
    return {key for key, film_ids in films.items() if len(film_ids) >= max_films_per_day}


async def sanity_holds(
    session: AsyncSession, *, changes: list[CompanyChangeRow], max_films_per_day: int
) -> tuple[list[CompanyChangeRow], list[CompanyChangeRow]]:
    """Split the quarantine's survivors into the ones that may card and the ones the burst
    check withholds. Returns `(eligible, held)`.

    Attachments only. A company *leaving* many films in one day is a studio's output being
    recatalogued or a duplicate company id being merged away — visible, reversible, and not the
    shape of defacement the check is for; withholding those detachments would suppress a real
    signal to guard against a hypothetical one.

    A `max_films_per_day` of 0 turns the check off. Unlike the credit half's thresholds that is
    permitted here rather than refused at boot, because nothing is being released: with no hold
    rows to strand, turning the check off just cards what it would have withheld.
    """
    if max_films_per_day <= 0 or not changes:
        return changes, []
    attachments = [c for c in changes if c.change == COMPANY_ADDED]
    burst = await _burst_days(
        session,
        company_days={(c.company_id, observation_day(c.changed_at)) for c in attachments},
        max_films_per_day=max_films_per_day,
    )
    if not burst:
        return changes, []
    eligible: list[CompanyChangeRow] = []
    held: list[CompanyChangeRow] = []
    for change in changes:
        if (
            change.change == COMPANY_ADDED
            and (
                change.company_id,
                observation_day(change.changed_at),
            )
            in burst
        ):
            held.append(change)
            log.info(
                "company burst hold: company %s on film %s (%s)",
                change.company_id,
                change.film_id,
                change.changed_at.isoformat(),
            )
        else:
            eligible.append(change)
    return eligible, held


def group_company_changes(changes: list[CompanyChangeRow]) -> list[CompanyGroup]:
    """One group — and so one event — per (film, event type) in this pass. Pure.

    Burst collapsing (D-7), on `group_attachments`' terms: the caller passes exactly the
    changes this pass will card, so "the pass" needs no key of its own, and `changed_at` is
    therefore not part of the key. A film that gained two studios two days apart and released
    both from quarantine together is one beat.

    One row per `(company, observation)` survives, which matters for the same reason it does on
    the credit side: a company that attached, detached and re-attached has two `added` rows in
    one backlog, and they are two beats rather than a duplicate — so the key keeps the
    observation, and whether the re-attachment shares a body with the attachment it repeats is
    `_uncarded_changes`' question, asked later and per company.
    """
    groups: dict[tuple[UUID, str], list[CompanyChangeRow]] = {}
    for change in changes:
        groups.setdefault((change.film_id, change.event_type), []).append(change)
    return [
        CompanyGroup(
            film_id=film_id,
            event_type=event_type,
            changes=_one_per_company_per_observation(
                sorted(grouped, key=lambda c: (c.name, c.company_id))
            ),
        )
        for (film_id, event_type), grouped in groups.items()
    ]


def _one_per_company_per_observation(
    changes: list[CompanyChangeRow],
) -> tuple[CompanyChangeRow, ...]:
    """The first change per `(company, changed_at)`, order preserved — see
    `group_company_changes`."""
    seen: set[tuple[int, datetime]] = set()
    kept: list[CompanyChangeRow] = []
    for change in changes:
        key = (change.company_id, change.changed_at)
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


async def _latest_company_event_types(session: AsyncSession, *, film_id: UUID) -> dict[int, str]:
    """Per-company latest event type among the two company types, keyed by company id. One
    query, no per-company roundtrips."""
    stmt = (
        select(Event.subject_key, Event.event_type)
        .where(
            Event.film_id == film_id,
            Event.event_type.in_(COMPANY_EVENT_TYPES),
            Event.subject_key.isnot(None),
        )
        .order_by(Event.occurred_at.desc(), Event.created_at.desc())
    )
    latest: dict[int, str] = {}
    for subject_key, event_type in await session.execute(stmt):
        for company_id in company_ids_in(subject_key):
            latest.setdefault(company_id, event_type)
    return latest


async def _uncarded_changes(
    session: AsyncSession, *, film_id: UUID, event_type: str, changes: tuple[CompanyChangeRow, ...]
) -> tuple[CompanyChangeRow, ...]:
    """The changes in a group that should still be carded for this beat.

    State-aware, the way `_uncarded_attachments` is: for each company, the most recent card
    among the two company types says what the reader was last told, and the change is
    suppressed only when that already says what this group would say. *The latest card for a
    company reflects its current attachment state* is the invariant, and it is what makes
    attach → detach → re-attach read as three cards while a re-read of the same rolling window
    adds none.

    A company is only ever suppressed individually: three studios attaching where one was
    already carded still cards the other two.

    **A departure the reader was never told to expect is suppressed too**, and that is the one
    place the two directions are not symmetric. A company with no card on this film at all is
    either attached since the film's baseline — in which case its departure is a complete beat,
    which is why there is no prior-attach-card gate here — or it attached inside this window and
    the attachment never published, which is what a reverted edit looks like from the far side.
    `attached_in_window` separates the two, and only the second is dropped: carding it would
    announce that a studio had left a film it was never reported as joining, which is precisely
    the correction-card the quarantine window exists to avoid publishing.
    """
    latest = await _latest_company_event_types(session, film_id=film_id)
    kept: list[CompanyChangeRow] = []
    for change in changes:
        carded_as = latest.get(change.company_id)
        if carded_as == event_type:
            continue
        if carded_as is None and change.attached_in_window:
            continue
        kept.append(change)
    return tuple(kept)


async def supersede_prior_company_cards(session: AsyncSession, *, removal: Event) -> int:
    """Mark the attach card each company named on `removal` was current on (D-2).

    Per company on the removal's `subject_key`: the most recent *published* `company_attached`
    card that occurred before the removal is set `superseded` with `superseded_by` pointing at
    the removal. Only the most recent one — an older card the same company is on was already
    the earlier claim, not the one this removal corrects. Nothing is hidden or deleted; the
    card keeps its place on every surface.

    The card, not the company, is the unit of supersession: `status` lives on the event row, so
    a card naming three studios is marked when any one of them leaves.

    Returns the number of cards marked. Caller owns the commit; `removal` must be flushed so
    its id exists for the FK.
    """
    # Resolve every target before marking any, for `supersede_prior_attachment_cards`' reason:
    # marking inside the loop would autoflush the first UPDATE ahead of the next company's
    # query, and a card two departing companies share would then fail the `published` filter
    # for the second — handing back an older card that company is on.
    targets: dict[UUID, Event] = {}
    for company_id in company_ids_in(removal.subject_key):
        token = company_subject_token(company_id)
        stmt = (
            select(Event)
            .where(
                Event.film_id == removal.film_id,
                Event.event_type == COMPANY_ATTACHED_EVENT_TYPE,
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


async def _card_group(session: AsyncSession, *, group: CompanyGroup) -> bool:
    """Create the event and its deterministic summary for one group, or report that it was
    already carded. One transaction covers both writes — and, for a removal, the supersession
    marks on the attach cards it corrects — so a card can never reach the feed without the
    summary row every read path inner-joins, nor a removal with its original still reading
    `published`. Caller owns the commit.

    Per-company suppression runs **before** the already-carded check, and the card is dated by
    what survives it: under burst collapsing the group's latest `changed_at` commonly belongs
    to a company the card will not name, and dating the card by the whole group would both
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
    attached = group.event_type == COMPANY_ATTACHED_EVENT_TYPE
    event = Event(
        film_id=group.film_id,
        event_type=group.event_type,
        confidence="rumored",
        provenance="catalog",
        occurred_at=occurred_at,
        region=None,
        subject_key=[company_subject_token(c.company_id) for c in changes],
    )
    session.add(event)
    await session.flush()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=(
            CompaniesAttached(companies=tuple(CompanyAttached(name=c.name) for c in changes))
            if attached
            else CompaniesDetached(companies=tuple(CompanyDetached(name=c.name) for c in changes))
        ),
        source_updated_at=event.updated_at,
    )
    if not attached:
        await supersede_prior_company_cards(session, removal=event)
    return True


async def run_company_events(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    now: datetime,
    lookback_days: int,
    quarantine_hours: int = 0,
    max_films_per_day: int = 0,
    failure_threshold: int = 10,
) -> CompanyEventResult:
    """Card every production-company attachment and detachment TMDB recorded in the window,
    less the ones quarantine and the burst check are still holding.

    Both gates default to off — `quarantine_hours` to 0 rather than the setting's 72, and the
    threshold likewise — the same way the credit phases' do: the caller that has a `Settings`
    passes the tuned values, and every other caller gets the plain behaviour it was written
    against. No TMDB client, unlike the credit attachment phase: the two checks that need one
    are the person-date checks, which have no company analogue.
    """
    result = CompanyEventResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)
    since = now - timedelta(days=lookback_days)

    async with owned_session(session_factory) as s:
        backlog = mark_window_attachments(await load_company_backlog(s, since=since))
        # Same session as the load: both gates read live `film_production_company` state
        # against the backlog just read, and a second session could straddle a refresh that
        # rebuilt those rows mid-check.
        eligible, held = await quarantine_company_changes(
            s, changes=backlog, now=now, quarantine_hours=quarantine_hours
        )
        eligible, burst_held = await sanity_holds(
            s, changes=eligible, max_films_per_day=max_films_per_day
        )
    # The whole backlog, not what survived the gates: "read 40, carded 2, held 37" is the shape
    # that says the phase is working, and netting those rows out of the read count would make a
    # quarantine that holds everything look like an empty window.
    result.changes_read = len(backlog)
    result.held = len(held)
    result.bursts_held = len(burst_held)
    groups = group_company_changes(eligible)
    log.info(
        "company events: %d changes in %d groups since %s (%d held, quarantine %dh; %d burst)",
        result.changes_read,
        len(groups),
        since.isoformat(),
        result.held,
        quarantine_hours,
        result.bursts_held,
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
            log.info("company group %s/%s was carded concurrently", group.film_id, group.event_type)
            result.skipped += 1
            guard.succeeded()
            continue
        except Exception:
            # One unwritable event must not cost the rest of the backlog.
            log.exception("carding companies for film %s failed", group.film_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
                log.error("company events: %s", result.abort_error)
                return result
            continue
        guard.succeeded()
        if created:
            result.events_created += 1
        else:
            result.skipped += 1

    log.info(
        "company events: %d created, %d already carded, %d held, %d burst held, %d failed",
        result.events_created,
        result.skipped,
        result.held,
        result.bursts_held,
        result.failures,
    )
    return result
