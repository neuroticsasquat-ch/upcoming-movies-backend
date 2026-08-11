"""The sweep's third phase: turn TMDB's own field changes into events (ADR-0014, spec §5.2).

The free half of catalog-sourced events. `catalog.film_field_change` already records every
semantic change to a `catalog.film` row, so a status crossing into production or
post-production needs no new history infrastructure — only a reader, and the deterministic
summary writer NEU-1080 built.

**Release dates are not read here** (NEU-1121). They were, from `film.release_date`, and that
was free and wrong: the primary date is the earliest release in any country of any type, while
the page shows US-or-origin theatrical dates, so the cards cited dates the page never showed.
They now come from `catalog.film_release_date_change` via `sweep.release_events`, which pays
for its own history exactly as the credit half does.

**Why it runs here.** Phase 2 (refresh) is what produces the changes this phase reads: undated
films sit outside the discover window, so nothing else re-reads them and no `film_field_change`
row is ever written for them. Reading straight after refreshing, in the same run, means a
change TMDB published today is carded today — and roughly two hours ahead of the daily chain,
so the card exists *before* `link` clusters that day's stories onto it (§6.1). Like refresh,
this phase is **not** gated by `SWEEP_ENABLED`: the master switch governs admission, and a film
already in the catalog getting an event admits nothing.

**First observation is a baseline** (§5.3). Free here, and only here: `film_field_change_trg`
is a `BEFORE UPDATE` trigger, so admitting a film writes no history and cannot card. The credit
half (NEU-1082) has to build that protection deliberately.

**Confidence is `confirmed`.** Unlike a credit — which any TMDB editor can add — `status` is a
field ADR-0002 already made TMDB the system of record for. A change to it is not a claim to be
corroborated; it *is* the corroboration.

Contract with the pipeline conventions, matching the other two phases: one session per item so
a failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures, and **no `finalize_run`** — all three phases share one `ingest_run` row
and the terminal status is the entrypoint's to write.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import FilmFieldChange
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.news.catalog_events import STATUS_EVENT_TYPES
from upmovies.news.models import Event
from upmovies.synthesize.deterministic import (
    CatalogChange,
    StatusChanged,
    write_deterministic_summary,
)

log = logging.getLogger(__name__)

# The one `catalog.film` column in scope. Everything else the trigger records (title, runtime,
# overview, …) is metadata drift, not a beat.
#
# `release_date` was here until NEU-1121 and is deliberately gone. That column is TMDB's
# *primary* date — the earliest release in any country of any type — while the film page shows
# US-or-origin theatrical dates only, so carding it produced cards citing dates the page never
# displayed. Its whole surface is now the year parenthetical after the title, and displayable
# release dates card from `catalog.film_release_date_change` instead (`sweep.release_events`).
# The consequence is accepted: the header year can change with nothing carding it.
TRACKED_FIELDS: tuple[str, ...] = ("status",)


@dataclass(frozen=True)
class TrackedChange:
    """One `catalog.film_field_change` row, as far as this phase is concerned."""

    id: int
    film_id: UUID
    field: str
    old_value: object
    new_value: object
    changed_at: datetime


@dataclass(frozen=True)
class CatalogFieldEvent:
    """What one `film_field_change` row cards as: the event type, and the change in the form
    the deterministic summary writer renders."""

    event_type: str
    change: CatalogChange


@dataclass
class FieldEventResult:
    """What one field-change pass read and wrote."""

    changes_read: int = 0
    events_created: int = 0
    skipped: int = 0
    """Changes that classified as a beat but were already carded — the re-run overlap doing
    its job, not a problem. A pass that is all `skipped` is the normal steady state for a
    catalog TMDB has not touched."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


def classify_field_change(
    field: str, old_value: object, new_value: object
) -> CatalogFieldEvent | None:
    """The one `film_field_change` row → event mapping. Pure — no DB, no clock.

    Returns None for every change that is not a beat, which is the overwhelming majority of
    what the trigger records.

    `release_date` is no longer handled here at all (NEU-1121) — the primary date is not the
    quantity the page shows, so it raises no events. Displayable release dates card from
    `catalog.film_release_date_change` via `sweep.release_events`.
    """
    if field == "status":
        if not isinstance(new_value, str):
            return None
        event_type = STATUS_EVENT_TYPES.get(new_value)
        if event_type is None:
            return None
        return CatalogFieldEvent(event_type, StatusChanged(new_status=new_value))
    return None


async def load_change_backlog(session: AsyncSession, *, since: datetime) -> list[TrackedChange]:
    """Every tracked-field change recorded at or after `since`, oldest first.

    Deliberately a fixed rolling window rather than a watermark off the last run. A watermark
    would advance past changes a *failed* sweep never got to, losing them permanently, and it
    buys nothing: `_already_carded` makes re-reading a change free, so the overlap costs two
    indexed queries per already-carded row and closes the hole outright.

    Plain dataclasses rather than ORM rows, as in the refresh phase: the backlog is read in
    one session and then worked through in a session per item, and a detached instance whose
    attributes happen to still be loaded is not a contract worth relying on.
    """
    stmt = (
        select(
            FilmFieldChange.id,
            FilmFieldChange.film_id,
            FilmFieldChange.field,
            FilmFieldChange.old_value,
            FilmFieldChange.new_value,
            FilmFieldChange.changed_at,
        )
        .where(FilmFieldChange.field.in_(TRACKED_FIELDS), FilmFieldChange.changed_at >= since)
        .order_by(FilmFieldChange.changed_at, FilmFieldChange.id)
    )
    return [
        TrackedChange(
            id=row.id,
            film_id=row.film_id,
            field=row.field,
            old_value=row.old_value,
            new_value=row.new_value,
            changed_at=row.changed_at,
        )
        for row in await session.execute(stmt)
    ]


async def _already_carded(
    session: AsyncSession,
    *,
    film_id: UUID,
    event_type: str,
) -> bool:
    """Whether this film already carries the event this change would card.

    Every type this phase still raises is a production milestone, so the whole rule is "does
    this film already have one", whatever raised it: a film enters production once, and any
    existing `production_start` is the card this change would duplicate. No window, no
    timestamp comparison, on either side (`ONCE_PER_FILM_EVENT_TYPES`).

    Release dates were the one type that needed more — matched on *when*, against both
    provenances, to honour the anti-double-card rule ADR-0014 owes ADR-0002. That logic moved
    to `sweep.release_events` with the dates themselves (NEU-1121); it is a rule about release
    dates and belongs where they are carded.
    """
    carded = exists().where(Event.film_id == film_id, Event.event_type == event_type)
    return bool((await session.execute(select(carded))).scalar())


async def _card_change(
    session: AsyncSession,
    *,
    change: TrackedChange,
    carded: CatalogFieldEvent,
) -> bool:
    """Create the event and its deterministic summary for one change, or report that it was
    already carded. One transaction covers both writes, so an event can never reach the feed
    without the summary row every read path inner-joins. Caller owns the commit."""
    if await _already_carded(
        session,
        film_id=change.film_id,
        event_type=carded.event_type,
    ):
        return False
    event = Event(
        film_id=change.film_id,
        event_type=carded.event_type,
        confidence="confirmed",
        provenance="catalog",
        # The change's own timestamp, not now: the card is dated when TMDB moved, so a
        # backlog worked through after an outage does not all land on the same day.
        occurred_at=change.changed_at,
        # Scope is the primary scalar `release_date` (ADR-0002); per-country dates have no
        # change history to read, so there is never a region to record.
        region=None,
    )
    session.add(event)
    await session.flush()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=carded.change,
        source_updated_at=event.updated_at,
    )
    return True


async def run_field_change_events(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    now: datetime,
    lookback_days: int,
    failure_threshold: int = 10,
) -> FieldEventResult:
    """Card every release-date and production-status change TMDB recorded in the window."""
    result = FieldEventResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    since = now - timedelta(days=lookback_days)

    async with owned_session(session_factory) as s:
        changes = await load_change_backlog(s, since=since)
    result.changes_read = len(changes)
    log.info("field events: %d tracked changes since %s", result.changes_read, since.isoformat())

    for change in changes:
        carded = classify_field_change(change.field, change.old_value, change.new_value)
        if carded is None:
            continue
        try:
            async with owned_session(session_factory) as s:
                created = await _card_change(
                    s,
                    change=change,
                    carded=carded,
                )
                if created:
                    await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except IntegrityError:
            # `uq_event_catalog_change` is the structural backstop under the skip check above,
            # so reaching it means a concurrent writer got there first — the change *is*
            # carded. Counting that as a failure would fail the run over the guarantee
            # working, and feed the abort guard on a healthy catalog.
            log.info("film_field_change %d was carded concurrently", change.id)
            result.skipped += 1
            guard.succeeded()
            continue
        except Exception:
            # One unwritable event must not cost the rest of the backlog.
            log.exception("carding film_field_change %d failed", change.id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
                log.error("field events: %s", result.abort_error)
                return result
            continue
        guard.succeeded()
        if created:
            result.events_created += 1
        else:
            result.skipped += 1

    log.info(
        "field events: %d created, %d already carded, %d failed",
        result.events_created,
        result.skipped,
        result.failures,
    )
    return result
