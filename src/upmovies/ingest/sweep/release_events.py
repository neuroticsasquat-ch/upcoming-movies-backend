"""The sweep phase that cards displayable release-date changes (NEU-1121, ADR-0014).

Reads `catalog.film_release_date_change` — the history `ingest.tmdb.release_date_history`
writes from the release-date rebuild — and turns each observation into one event.

**Why this exists as its own phase.** Release-date events used to come free from
`sweep.field_events`, off the `film_field_change` trigger on `catalog.film.release_date`. Free
and wrong: that column is TMDB's *primary* date, the earliest release in any country of any
type, while the film page lists US-or-origin theatrical dates. The card routinely named a date
the page never showed, and on 2026-08-11 a fifth of them were for films whose page showed no
release date at all. The subject is now a **displayable** row, which needs history of its own,
which is why this half looks like the credit half rather than the field half.

**One event per observation, not per market.** `uq_event_catalog_change` permits one catalog
event per (film, type, timestamp), and the rebuild finds every change in one observation, so US
limited and US wide moving in one distributor announcement share a card. They usually do move
together — two cards would be two beats where the world had one. Which markets moved is carried
in `subject_key` as `US:wide`-style tokens and named in the body.

**Confidence is `confirmed`**, matching what the field phase gives `status` and unlike the
credit half's `rumored`: a release date is the field ADR-0002 already makes TMDB the system of
record for, and the displayable cut means an anonymous editor adding a foreign premiere no
longer reaches this path at all.

Contract with the pipeline conventions, matching the other phases: one session per item so a
failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures, and **no `finalize_run`** — every phase shares one `ingest_run` row.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import FilmReleaseDateChange
from upmovies.catalog.release_grade import release_bucket
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.release_date_history import RELEASE_DATE_MOVED
from upmovies.news.models import Event
from upmovies.synthesize.deterministic import (
    ReleaseDateChanged,
    ReleaseDatesChanged,
    write_deterministic_summary,
)

log = logging.getLogger(__name__)

EVENT_TYPE = "release_date"


@dataclass(frozen=True)
class ReleaseDateMove:
    """One row of `film_release_date_change`, as far as this phase is concerned."""

    film_id: UUID
    iso_3166_1: str
    release_type: int
    previous_date: date | None
    new_date: date
    change: str
    changed_at: datetime


@dataclass(frozen=True)
class ReleaseDateGroup:
    """Every displayable date one observation of one film moved — and so one event."""

    film_id: UUID
    changed_at: datetime
    moves: tuple[ReleaseDateMove, ...]

    @property
    def region(self) -> str:
        """The single region the card is tagged with, for `_region_visible()`.

        A group can span markets (a US date and an origin-country date moving together) and
        `Event.region` holds one value, so `US` wins when present. That is the safe direction:
        `US` is in every film's displayable set, so the event stays visible for exactly the
        audience the page's own release list serves.
        """
        regions = {m.iso_3166_1 for m in self.moves}
        return "US" if "US" in regions else sorted(regions)[0]

    @property
    def subject_keys(self) -> list[str]:
        """`region:label` per market — what the card is *about*, in the form a later story
        path can match against so the same move does not open a second card."""
        return [
            f"{m.iso_3166_1}:{release_bucket(m.release_type) or str(m.release_type)}"
            for m in self.moves
        ]


@dataclass
class ReleaseEventResult:
    """What one release-date pass read and wrote."""

    changes_read: int = 0
    events_created: int = 0
    skipped: int = 0
    """Observations already carded — the rolling window's re-read doing its job."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None
    markets: set[str] = field(default_factory=set)
    """Distinct `region:label` subjects touched this pass. Cheap, and the honest answer to
    "which markets is the catalog actually moving" once the tranches widen."""


async def load_release_change_backlog(
    session: AsyncSession, *, since: datetime
) -> list[ReleaseDateMove]:
    """Every recorded displayable release-date change at or after `since`, oldest first.

    A fixed rolling window rather than a watermark, for the reason the field phase documents:
    a watermark advances past changes a *failed* sweep never carded, losing them permanently,
    while re-reading a carded change is free.
    """
    stmt = (
        select(
            FilmReleaseDateChange.film_id,
            FilmReleaseDateChange.iso_3166_1,
            FilmReleaseDateChange.release_type,
            FilmReleaseDateChange.previous_date,
            FilmReleaseDateChange.new_date,
            FilmReleaseDateChange.change,
            FilmReleaseDateChange.changed_at,
        )
        .where(FilmReleaseDateChange.changed_at >= since)
        .order_by(FilmReleaseDateChange.changed_at, FilmReleaseDateChange.id)
    )
    return [
        ReleaseDateMove(
            film_id=row.film_id,
            iso_3166_1=row.iso_3166_1,
            release_type=row.release_type,
            previous_date=row.previous_date,
            new_date=row.new_date,
            change=row.change,
            changed_at=row.changed_at,
        )
        for row in await session.execute(stmt)
    ]


def group_moves(moves: list[ReleaseDateMove]) -> list[ReleaseDateGroup]:
    """One group — and so one event — per (film, observation). Pure.

    Keyed on `changed_at` rather than on the run, because that timestamp is what `occurred_at`
    records and what `uq_event_catalog_change` keys on: two observations stay two cards however
    close together the sweep reads them.
    """
    grouped: dict[tuple[UUID, datetime], list[ReleaseDateMove]] = {}
    for move in moves:
        grouped.setdefault((move.film_id, move.changed_at), []).append(move)
    return [
        ReleaseDateGroup(film_id=film_id, changed_at=changed_at, moves=tuple(items))
        for (film_id, changed_at), items in grouped.items()
    ]


def render_change(move: ReleaseDateMove) -> ReleaseDateChanged:
    """One stored row in the form the deterministic writer renders."""
    return ReleaseDateChanged(
        region=move.iso_3166_1,
        label=release_bucket(move.release_type) or str(move.release_type),
        new_date=move.new_date,
        previous_date=move.previous_date if move.change == RELEASE_DATE_MOVED else None,
    )


async def _already_carded(
    session: AsyncSession, *, film_id: UUID, changed_at: datetime, corroboration_window_days: int
) -> bool:
    """Whether this film already carries the event this observation would card.

    Two jobs, inherited from `field_events` when release dates moved out of it (NEU-1121) —
    the rule is about release dates, so it belongs wherever they are carded. It is the
    **idempotency** guard, since the rolling window re-reads every change for days, and it is
    the **anti-double-card** rule ADR-0014 owes ADR-0002: the story-triggered path and this one
    must not both raise an event for one date move.

    The two provenances are asked different questions on purpose:

    - **catalog**, exactly at `changed_at` — that is the timestamp this phase writes, so it is
      this very observation's own card and nothing else's. Matching those on a window instead
      would silently swallow a second genuine date move inside `W`.
    - **story**, anywhere within `W` either side — `link` dates that event to the story's
      publication rather than to the change, and may only form it when a change lands within
      `W`, so a story-borne release-date event inside the window is reporting this same move.

    The mirror of this lives in `link.cluster._catalog_dedup_target`, which decides where a
    corroborated story lands. Both must move together or one direction reopens.
    """
    window = timedelta(days=corroboration_window_days)
    carded = exists().where(
        Event.film_id == film_id,
        Event.event_type == EVENT_TYPE,
        or_(
            and_(Event.provenance == "catalog", Event.occurred_at == changed_at),
            and_(
                Event.provenance == "story",
                Event.occurred_at.between(changed_at - window, changed_at + window),
            ),
        ),
    )
    return bool((await session.execute(select(carded))).scalar())


async def _card_group(
    session: AsyncSession, *, group: ReleaseDateGroup, corroboration_window_days: int
) -> bool:
    """Create the event and its summary for one group, or report it already carded. One
    transaction covers both writes, so an event never reaches the feed without the summary row
    every read path inner-joins. Caller owns the commit."""
    if await _already_carded(
        session,
        film_id=group.film_id,
        changed_at=group.changed_at,
        corroboration_window_days=corroboration_window_days,
    ):
        return False
    event = Event(
        film_id=group.film_id,
        event_type=EVENT_TYPE,
        confidence="confirmed",
        provenance="catalog",
        # The observation's own timestamp, not now, so a backlog worked through after an
        # outage does not all land on one day.
        occurred_at=group.changed_at,
        region=group.region,
        subject_key=group.subject_keys,
    )
    session.add(event)
    await session.flush()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDatesChanged(changes=tuple(render_change(m) for m in group.moves)),
        source_updated_at=event.updated_at,
    )
    return True


async def run_release_date_events(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    now: datetime,
    lookback_days: int,
    corroboration_window_days: int,
    failure_threshold: int = 10,
) -> ReleaseEventResult:
    """Card every displayable release-date change recorded in the window."""
    result = ReleaseEventResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    since = now - timedelta(days=lookback_days)

    async with owned_session(session_factory) as s:
        moves = await load_release_change_backlog(s, since=since)
    result.changes_read = len(moves)
    groups = group_moves(moves)
    log.info(
        "release events: %d changes in %d groups since %s",
        result.changes_read,
        len(groups),
        since.isoformat(),
    )

    for group in groups:
        try:
            async with owned_session(session_factory) as s:
                created = await _card_group(
                    s, group=group, corroboration_window_days=corroboration_window_days
                )
                if created:
                    await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except IntegrityError:
            # The structural backstop under the skip check fired, so a concurrent writer got
            # there first and the group *is* carded. Not a failure.
            log.info("release group %s@%s was carded concurrently", group.film_id, group.changed_at)
            result.skipped += 1
            guard.succeeded()
            continue
        except Exception:
            # One unwritable event must not cost the rest of the backlog.
            log.exception("carding release dates for film %s failed", group.film_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
                log.error("release events: %s", result.abort_error)
                return result
            continue
        guard.succeeded()
        if created:
            result.events_created += 1
            result.markets.update(group.subject_keys)
        else:
            result.skipped += 1

    log.info(
        "release events: %d carded from %d changes, %d skipped, %d failed, markets %s",
        result.events_created,
        result.changes_read,
        result.skipped,
        result.failures,
        sorted(result.markets),
    )
    return result
