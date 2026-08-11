"""The sweep's refresh phase: re-fetch every in-play film discover did not touch, so the
catalog keeps moving for films TMDB's dated roster cannot reach (spec §4.5, §6.2).

**This is the phase the project silently fails without.** Undated films sit outside the
discover window, so the `tmdb` stage never reads them again after admission — and no refetch
means no upsert, no upsert means no `catalog.film_field_change` row, and every
catalog-sourced event produces nothing. A run that enumerates perfectly and refreshes
nothing looks healthy and delivers the feature's whole point to no one, which is why the
counts here are reported distinctly on `/admin/runs` (`summary.sweep_detail`).

**Scoped by reachability, not by datedness.** The refresh set is every film whose
`updated_at` predates the last `tmdb` run — no popularity branch, no
undated/dated branch. That is what closes the *promotion gap*: an admitted undated film
that finally gets a date is no longer undated, but is still below the popularity floor
where `_discover_candidate_ids` stops paging, so discover never reaches it either. A
datedness-scoped refresh would drop it and freeze its metadata permanently. Scoping this
way also self-corrects if the discover floor ever moves.

**Dormant films are not exempt**, they run on a reduced cadence. Detecting the change that
revives a dormant film requires re-fetching it, so exempting them would make dormancy a
one-way door with no handle on the other side (ADR-0015).

**Not gated by `SWEEP_ENABLED`.** The master switch governs *admission* — whether the sweep
may write films it did not previously hold (§7.3). Refreshing a film the catalog already
carries admits nothing, and gating it would mean the shipped default configuration, with
every flag off, never refreshes anything and the catalog-sourced-event feature never works
at all. The rollback story is unaffected: turning the sweep off stops new admissions, and
dormancy still drains what is already in, on the reduced cadence §4.5 prices in.

Contract with the pipeline conventions, matching the enumerate phase: commit per item so one
failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures — and **no `finalize_run`**, because both phases share one `ingest_run`
row and the terminal status is the entrypoint's to write (NEU-1079).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

import httpx
from sqlalchemy import ColumnElement, and_, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.catalog.queries import dormant_film_clause, in_play_clause
from upmovies.ingest.runs import last_finished_run_started_at, record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.client import TMDBClient, TMDBNotFound
from upmovies.ingest.tmdb.upsert import mark_film_missing, upsert_film

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshTarget:
    """One film the refresh phase will re-fetch, and why it was due."""

    tmdb_id: int
    dormant: bool


@dataclass
class RefreshResult:
    """What one refresh phase selected and wrote."""

    selected: int = 0
    dormant_selected: int = 0
    """How many of `selected` came in on the reduced cadence rather than the discover
    watermark — the number that says the dormant carve-out is actually running."""
    refreshed: int = 0
    missing: int = 0
    """Films TMDB answered 404 for, and this pass tombstoned. Reported apart from `failures`
    because they are the opposite kind of event: a failure is a reason to worry about TMDB, a
    missing film is a reason to stop asking about it."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


def refresh_set_clause(
    *,
    today: date,
    excluded_statuses: frozenset[str],
    dormancy_days: int,
    dormant_refresh_days: int,
    discover_watermark: datetime | None,
) -> ColumnElement[bool]:
    """WHERE predicate selecting the films this pass owes a re-fetch.

    In play (neither released nor called off), and then one of *three* cadences. A live film
    discover can still reach is due when its `updated_at` predates `discover_watermark`; a live
    dormant one is due when it predates `dormant_refresh_days` ago; and a film tombstoned as
    missing from TMDB is due when its **tombstone** predates `dormant_refresh_days` ago.

    The third cadence is what makes a 404 cost one request per `dormant_refresh_days` rather
    than one per pass forever. Without a tombstone at all, a deleted id is not merely retried,
    it is retried *first* — the order below is stalest-first, and a film that cannot be fetched
    never has its `updated_at` bumped, so it holds the head of the queue permanently (NEU-1124).

    Note the tombstoned branch is keyed on `tmdb_missing_at` alone, ignoring `updated_at`
    entirely: a missing film's `updated_at` is frozen at its last successful read and would put
    it at the head of the queue on every pass, which is the behaviour being fixed. And it is a
    reduced cadence rather than a permanent exclusion for the reason §4.5 gives about dormancy —
    detecting that TMDB restored an entry requires asking TMDB about it, so suppressing the only
    reader would make the tombstone a one-way door.

    A `discover_watermark` of None means no `tmdb` run has ever finished, so nothing can be
    assumed refreshed and every non-dormant film in play is due. Dormant films keep their own
    cadence either way — they are undated by definition, so discover never touched them and
    the watermark says nothing about them.
    """
    dormant = dormant_film_clause(today=today, dormancy_days=dormancy_days)
    dormant_due_before = datetime.combine(
        today - timedelta(days=dormant_refresh_days), time.min, tzinfo=UTC
    )
    discover_due = true() if discover_watermark is None else Film.updated_at < discover_watermark
    return and_(
        in_play_clause(today=today, excluded_statuses=excluded_statuses),
        or_(
            and_(
                Film.tmdb_missing_at.is_(None),
                or_(
                    and_(not_(dormant), discover_due),
                    and_(dormant, Film.updated_at < dormant_due_before),
                ),
            ),
            Film.tmdb_missing_at < dormant_due_before,
        ),
    )


async def load_refresh_set(
    session: AsyncSession,
    *,
    today: date,
    excluded_statuses: frozenset[str],
    dormancy_days: int,
    dormant_refresh_days: int,
    discover_watermark: datetime | None,
) -> list[RefreshTarget]:
    """The films due a re-fetch, stalest first so an aborted pass has done the most good."""
    dormant = dormant_film_clause(today=today, dormancy_days=dormancy_days)
    stmt = (
        select(Film.tmdb_id, dormant.label("dormant"))
        .where(
            refresh_set_clause(
                today=today,
                excluded_statuses=excluded_statuses,
                dormancy_days=dormancy_days,
                dormant_refresh_days=dormant_refresh_days,
                discover_watermark=discover_watermark,
            )
        )
        .order_by(Film.updated_at, Film.tmdb_id)
    )
    rows = await session.execute(stmt)
    return [RefreshTarget(tmdb_id=tmdb_id, dormant=is_dormant) for tmdb_id, is_dormant in rows]


async def run_sweep_refresh(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    today: date,
    excluded_statuses: frozenset[str],
    dormancy_days: int,
    dormant_refresh_days: int,
    failure_threshold: int = 10,
    log_every: int = 250,
) -> RefreshResult:
    """Re-fetch every in-play film the last discover pass did not reach."""
    result = RefreshResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        watermark = await last_finished_run_started_at(s, "tmdb")
        targets = await load_refresh_set(
            s,
            today=today,
            excluded_statuses=excluded_statuses,
            dormancy_days=dormancy_days,
            dormant_refresh_days=dormant_refresh_days,
            discover_watermark=watermark,
        )
    result.selected = len(targets)
    result.dormant_selected = sum(1 for t in targets if t.dormant)
    log.info(
        "refresh: %d films due (%d dormant), discover watermark %s",
        result.selected,
        result.dormant_selected,
        watermark.isoformat() if watermark is not None else "none",
    )

    await _refresh_films(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        targets=targets,
        result=result,
        guard=guard,
        heartbeat=heartbeat,
        log_every=log_every,
    )
    log.info(
        "refresh: %d refreshed, %d missing, %d failed",
        result.refreshed,
        result.missing,
        result.failures,
    )
    return result


async def _refresh_films(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    targets: list[RefreshTarget],
    result: RefreshResult,
    guard: AbortGuard,
    heartbeat: Heartbeat,
    log_every: int,
) -> None:
    """One `/movie/{id}` per film, straight into the existing upsert.

    Nothing is judged on the way in. A film TMDB now reports `Released` or `Canceled` is
    upserted like any other — that transition is precisely the change the refresh exists to
    record, and `active_film_clause` drops the film from the next pass on its own.
    """
    for i, target in enumerate(targets, start=1):
        await heartbeat.tick()
        try:
            details = await client.movie_details(target.tmdb_id)
            async with owned_session(session_factory) as s:
                await upsert_film(s, details)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            result.refreshed += 1
            guard.succeeded()
            if i % log_every == 0:
                log.info("refresh: %d/%d films", i, len(targets))
            continue
        except TMDBNotFound:
            # Terminal, not an outage — so it is tombstoned rather than retried, and it does
            # not touch `guard` in *either* direction. Not `failed()`, because a permanent
            # deletion is not evidence TMDB is down: eleven of them at the head of this queue
            # is what aborted the 2026-08-11 sweep. Not `succeeded()` either, because a 404
            # interleaved with real failures is no evidence TMDB has recovered, and resetting
            # the streak on one would blunt the outage abort this guard exists for.
            async with owned_session(session_factory) as s:
                await mark_film_missing(s, target.tmdb_id)
                # Counted as processed, like any other item the pass disposed of for good —
                # the same call the zero-candidate rejection makes for a story it rejects
                # without a model call (CONTEXT.md, ADR-0009).
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            result.missing += 1
            log.info("refresh: film %d is gone from TMDB (404); tombstoned", target.tmdb_id)
            continue
        except httpx.HTTPError as e:
            log.warning("refreshing film %d failed: %s", target.tmdb_id, e)
        except Exception:
            # One malformed payload must not cost the rest of the catalog.
            log.exception("unexpected error refreshing film %d", target.tmdb_id)
        result.failures += 1
        if await guard.failed():
            result.aborted = True
            result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
            log.error("refresh: %s", result.abort_error)
            return
