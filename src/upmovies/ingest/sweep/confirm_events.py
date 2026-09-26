"""The sweep's confirmation phase: a story card the catalog has caught up with stops being a
rumor (EF-10, D-1446.4).

A story-formed attach card publishes the day the trades run the beat, at `rumored` — "in
talks". It is a claim, not an observation, and it stays marked Unconfirmed — on the timeline
and in the digest (DC-5) — until the catalog confirms it. `news.attachment_confirm` already
links the two: when TMDB observes the
change the story predicted, the change row is stamped `carded_by_event_id` with the card that
published it. Nothing until this phase read that stamp back the other way.

**The flip happens here rather than at stamp time**, and the difference is quarantine. The
backward stamp runs the moment the change is observed, *before* `SWEEP_CREDIT_QUARANTINE_HOURS`
has passed, because stamping is what takes the row out of the carding backlog and the loader
runs first. Confirming there would vouch for a change that may still be reverted — which is
the entire thing quarantine exists to prevent. So the stamp is early and unconditional,
and this phase comes back for the aged rows and re-checks live state:

- an `added` row confirms when the attachment is **still there**;
- a `removed` row confirms when it is **still gone**;
- a change TMDB reverted confirms nothing, and the row is looked at again next pass. It stays
  stamped: the story still published the beat, and un-stamping it would let the sweep card a
  duplicate.

**A confirmed detachment supersedes the attachment it contradicts** (D-1446.5). As a rumor it
supersedes nothing — a wrong trade would hide a true attachment — but a detachment the catalog
has confirmed is a detachment, and the attach card it corrects is marked exactly as a catalog
detach would have marked it (D-2).

**What the flip is for.** It is a state change on the card, not a delivery trigger: the
timeline and the digest's Unconfirmed pill read `confidence`, so the card stops saying "in
talks" wherever it is next shown. It was once also the trigger for a push (EF-10); ADR-0021
retired the push, and a reader who already has the card in a digest gets no second line for
it — the notify pass windows on `created_at` alone.

Contract with the pipeline conventions, matching the other phases: one session per item so a
failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures, and **no `finalize_run`** — all phases share one `ingest_run` row.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.follow_queries import followed_people
from upmovies.catalog.models import (
    COLLECTION_FIELD,
    Film,
    FilmCompanyChange,
    FilmCreditChange,
    FilmFieldChange,
    FilmProductionCompany,
)
from upmovies.catalog.queries import present_recorded_credits
from upmovies.catalog.seed_grade import recorded_credit_key
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.collection_events import supersede_prior_collection_cards
from upmovies.ingest.sweep.company_events import supersede_prior_company_cards
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.company_history import COMPANY_ADDED
from upmovies.ingest.tmdb.credit_history import CREDIT_ADDED
from upmovies.news.catalog_events import (
    COLLECTION_ADDED,
    collection_field_events,
)
from upmovies.news.models import Event

log = logging.getLogger(__name__)

_RUMORED = "rumored"
_CONFIRMED = "confirmed"


@dataclass
class ConfirmEventResult:
    """What one confirmation pass read and wrote."""

    stamped_read: int = 0
    """Aged, stamped change rows whose card is still a rumor — the candidates."""
    cards_confirmed: int = 0
    """Cards flipped to `confirmed`. The rest are changes TMDB has since reverted, which this
    phase deliberately leaves alone for the next pass."""
    cards_superseded: int = 0
    """Attach cards marked `superseded` by a detach card confirming (D-1446.5)."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


@dataclass(frozen=True)
class _Candidate:
    """One stamped change row, aged past quarantine, whose card is a story rumor."""

    card: Event
    attached: bool
    """Whether the row records the entity *arriving*. Confirmation is "live state still agrees
    with the row", which for an arrival means the attachment is present and for a departure
    means it is absent — the two are not the same test and cannot be folded into one flag on
    the card."""
    present: bool
    """Whether the attachment is standing in the catalog right now."""
    entity_id: int | None
    """The company or collection the card is about, for the supersession a confirmed
    detachment performs. None for a person: no story-formed `credit_removed` card can exist,
    because the story vocabulary has no such type."""
    kind: str


async def confirm_stamped_cards(
    session: AsyncSession, *, now: datetime, quarantine: timedelta
) -> tuple[int, int, int]:
    """Flip every aged, stamped, still-rumored story card the catalog now agrees with.
    Returns `(stamped rows read, cards confirmed, attach cards superseded)`.

    The read count comes back with the other two rather than being gathered by the caller,
    because gathering it would mean running the same three queries twice — and twice over a
    window whose whole point is that it re-presents the same rows on every pass.

    Idempotent, by construction rather than by a marker: a card already `confirmed` is not
    selected, so a second pass over the same rows does nothing. A card whose change was
    reverted is selected again next pass and flips then if the attachment has returned.

    One card can be reached by several stamped rows — a story naming two studios that TMDB then
    observes separately — and is flipped once; `updated_at` is set from the single `now` this
    pass was given, so the notify window sees one moment rather than a smear.
    """
    candidates = await _aged_candidates(session, now=now, quarantine=quarantine)
    confirmed: dict[UUID, Event] = {}
    superseded = 0
    for candidate in candidates:
        if candidate.present != candidate.attached:
            continue
        card = candidate.card
        if card.id not in confirmed:
            card.confidence = _CONFIRMED
            card.updated_at = now
            confirmed[card.id] = card
        if not candidate.attached and candidate.entity_id is not None:
            superseded += await _supersede(session, card, candidate)
    if confirmed:
        await session.flush()
        log.info(
            "confirmation: %d story card(s) confirmed, %d attach card(s) superseded",
            len(confirmed),
            superseded,
        )
    return len(candidates), len(confirmed), superseded


async def _supersede(session: AsyncSession, card: Event, candidate: _Candidate) -> int:
    """The supersession a confirmed detachment performs (D-1446.5), routed by kind.

    The entity is named explicitly rather than read off `card.subject_key`, because a story
    card carries no organisation token — see `news.supersede`."""
    assert candidate.entity_id is not None
    if candidate.kind == "company":
        return await supersede_prior_company_cards(
            session, removal=card, company_ids=[candidate.entity_id]
        )
    return await supersede_prior_collection_cards(
        session, removal=card, collection_ids=[candidate.entity_id]
    )


async def _aged_candidates(
    session: AsyncSession, *, now: datetime, quarantine: timedelta
) -> list[_Candidate]:
    """Every stamped change row older than the quarantine window whose card is still a story
    rumor, with live state read beside it.

    Three queries — one per change table — joined to `news.event` on the stamp, plus one
    presence read per kind over the films they name. Not one query per row: the rolling window
    re-presents the same rows on every pass for as long as a card stays rumored, so a per-row
    lookup would make a quiet window cost the same as a busy one.
    """
    floor = now - quarantine
    candidates: list[_Candidate] = []

    def rumored[T: tuple[Any, ...]](stmt: Select[T]) -> Select[T]:
        """The three terms that make a stamped row's card a candidate, spelled once. Story
        provenance and `rumored` are what "still a rumor" means; `published` keeps a card a
        detachment already superseded out of it."""
        return stmt.where(
            Event.provenance == "story",
            Event.confidence == _RUMORED,
            Event.status == "published",
        )

    credits = (
        await session.execute(
            rumored(
                select(FilmCreditChange, Event)
                .join(Event, Event.id == FilmCreditChange.carded_by_event_id)
                .where(FilmCreditChange.changed_at <= floor)
            )
        )
    ).all()
    if credits:
        followed = set((await session.execute(followed_people())).scalars().all())
        present = await present_recorded_credits(
            session, film_ids={row.film_id for row, _ in credits}, followed=followed
        )
        for row, card in credits:
            key = (row.film_id, row.person_id, *recorded_credit_key(row.credit_type, row.job))
            candidates.append(
                _Candidate(
                    card=card,
                    attached=row.change == CREDIT_ADDED,
                    present=key in present,
                    entity_id=None,
                    kind="person",
                )
            )

    companies = (
        await session.execute(
            rumored(
                select(FilmCompanyChange, Event)
                .join(Event, Event.id == FilmCompanyChange.carded_by_event_id)
                .where(FilmCompanyChange.changed_at <= floor)
            )
        )
    ).all()
    if companies:
        held = {
            (film_id, company_id)
            for film_id, company_id in await session.execute(
                select(FilmProductionCompany.film_id, FilmProductionCompany.company_id).where(
                    FilmProductionCompany.film_id.in_({row.film_id for row, _ in companies})
                )
            )
        }
        for row, card in companies:
            candidates.append(
                _Candidate(
                    card=card,
                    attached=row.change == COMPANY_ADDED,
                    present=(row.film_id, row.company_id) in held,
                    entity_id=row.company_id,
                    kind="company",
                )
            )

    collections = (
        await session.execute(
            rumored(
                select(FilmFieldChange, Event)
                .join(Event, Event.id == FilmFieldChange.carded_by_event_id)
                .where(
                    FilmFieldChange.field == COLLECTION_FIELD,
                    FilmFieldChange.changed_at <= floor,
                )
            )
        )
    ).all()
    if collections:
        current = {
            film_id: collection_id
            for film_id, collection_id in await session.execute(
                select(Film.id, Film.collection_id).where(
                    Film.id.in_({row.film_id for row, _ in collections})
                )
            )
        }
        for row, card in collections:
            beats = collection_field_events(row.old_value, row.new_value)
            # A move (`id -> id'`) is never stamped — one row, two beats, one stamp column —
            # so a stamped row always resolves to exactly one direction. Guarded rather than
            # indexed blindly: the stamp is durable and the rule that writes it is not.
            if len(beats) != 1:
                continue
            direction, collection_id = beats[0]
            candidates.append(
                _Candidate(
                    card=card,
                    attached=direction == COLLECTION_ADDED,
                    present=current.get(row.film_id) == collection_id,
                    entity_id=collection_id,
                    kind="collection",
                )
            )
    return candidates


async def run_confirmation_events(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    now: datetime,
    quarantine_hours: int,
    failure_threshold: int,
) -> ConfirmEventResult:
    """The confirmation phase: flip the story cards the catalog has caught up with.

    One pass over one session, unlike the carding phases' session-per-item: the work is a
    single bounded read of stamped rows and a handful of UPDATEs, and the rows are related —
    two studios on one card confirm together or not at all. The abort guard is still here so a
    crash is reported the way every other phase reports one, and so the run's failure count
    means the same thing across phases.

    `quarantine_hours` is `SWEEP_CREDIT_QUARANTINE_HOURS`, the one quarantine setting all three
    kinds share (M2). A value of 0 disables the wait, exactly as it does in the carding phases,
    and every stamped row is then eligible on the pass that stamped it.
    """
    result = ConfirmEventResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)
    await heartbeat.tick()
    try:
        async with owned_session(session_factory) as s:
            read, confirmed, superseded = await confirm_stamped_cards(
                s, now=now, quarantine=timedelta(hours=quarantine_hours)
            )
            if confirmed:
                await record_progress(s, run_id, processed_delta=confirmed)
            await s.commit()
        result.stamped_read = read
        result.cards_confirmed = confirmed
        result.cards_superseded = superseded
    except Exception as e:
        log.exception("confirmation phase failed")
        result.failures += 1
        if await guard.failed():
            result.aborted = True
            result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
        else:
            result.abort_error = str(e)
    log.info(
        "confirmation: %d stamped rows read, %d cards confirmed, %d superseded, %d failed",
        result.stamped_read,
        result.cards_confirmed,
        result.cards_superseded,
        result.failures,
    )
    return result
