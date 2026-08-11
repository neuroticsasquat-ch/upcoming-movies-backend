"""The sweep's fourth phase: turn TMDB's credit attachments into events (ADR-0014, spec §5.2).

The payoff half of catalog-sourced events, and the expensive one — `catalog.film_credit` is
delete-and-rebuilt on every ingest, so NEU-1082 had to build the history this phase reads.
It is also the one the project was largely built for: a director attaching to an undated film
is the "this is real now" beat no trade has written about yet.

**Confidence is `rumored`**, unlike the field-change phase's `confirmed`. ADR-0002 makes TMDB
the system of record for its own scalar fields, so a `release_date` move *is* the corroboration
— but a credit is community-edited, and one added by an anonymous editor is not a studio
announcement.

**First observation is a baseline** (§5.3) is inherited, not re-implemented: `film_credit_change`
holds no rows at all for a film whose credits the catalog had never observed, so there is
nothing here to read. The integration tests assert it anyway — it is the failure that would be
most visible in production, and this phase is where it would surface.

**One observation is one card.** TMDB routinely gains a whole top-billed cast between two
ingests. The grouping below is the difference between one `casting` card naming three people
and three cards about one beat, and `uq_event_catalog_change` enforces the same thing
structurally: one catalog event per film, type and timestamp.

Contract with the pipeline conventions, matching the other phases: one session per item
so a failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures, and **no `finalize_run`** — all five phases share one `ingest_run` row.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import FilmCreditChange, Person
from upmovies.catalog.seed_grade import crew_role
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.credit_history import CREDIT_ADDED
from upmovies.news.catalog_events import CREDIT_ROLE_EVENT_TYPES
from upmovies.news.models import Event
from upmovies.news.subject_key import normalize_name, recorded_subject_names
from upmovies.synthesize.deterministic import (
    CreditAttached,
    CreditsAttached,
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


async def _uncarded_credits(
    session: AsyncSession, *, film_id: UUID, event_type: str, credits: tuple[CreditAttached, ...]
) -> tuple[CreditAttached, ...]:
    """The credits in a group naming someone this film has not already carded *for this beat*.

    The anti-double-card rule for the credit half, and the mirror of what
    `link.cluster._catalog_dedup_target` does from the other end. A trade story that broke a
    casting a week before TMDB recorded it already produced a `casting` event carrying that
    performer in `subject_key`; carding the credit too would put the same attachment on the
    feed twice, the second time with no sources. Matching is on the shared normalized name
    (`news.subject_key`) — the two sides must agree on what "the same person" is.

    Scoped to the group's **own** event type, and not to credit events generally: an
    actor-director is carded once for joining the cast and again, legitimately, when TMDB
    records them as directing. Suppressing across types would drop the second — which is the
    `crew_attached` beat this phase exists to raise.

    A person is only ever suppressed *individually*: three cast arriving where one was
    already carded still cards the other two.
    """
    recorded = await recorded_subject_names(session, film_id=film_id, event_types=(event_type,))
    return tuple(c for c in credits if normalize_name(c.name) not in recorded)


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
        # TMDB is community-edited: a credit added by an anonymous editor is a claim, not a
        # studio announcement (§5.4). Deliberately below the field-change phase's `confirmed`.
        confidence="rumored",
        provenance="catalog",
        # The observation's own timestamp, not now, so a backlog worked through after an
        # outage does not all land on the same day.
        occurred_at=group.changed_at,
        region=None,
        # What the card is *about*, in the form both paths match on — this is what stops a
        # trade story about the same attachment opening a second card.
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
    failure_threshold: int = 10,
) -> CreditEventResult:
    """Card every seed-grade credit attachment TMDB recorded in the window."""
    result = CreditEventResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)
    since = now - timedelta(days=lookback_days)

    async with owned_session(session_factory) as s:
        attachments = await load_attachment_backlog(s, since=since)
    result.attachments_read = len(attachments)
    groups = group_attachments(attachments)
    log.info(
        "credit events: %d attachments in %d groups since %s",
        result.attachments_read,
        len(groups),
        since.isoformat(),
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
        "credit events: %d created, %d already carded, %d failed",
        result.events_created,
        result.skipped,
        result.failures,
    )
    return result
