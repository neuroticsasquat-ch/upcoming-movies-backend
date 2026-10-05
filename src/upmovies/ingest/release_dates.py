"""The release-date poll: a released film's release slate, re-read once a day so a US home
release dated after the film opened can still card (D-26, NEU-1532).

**Why it exists.** D-26 promised a `release_date` card when a US digital or physical date is set
after release, and nothing could raise one: the sweep's refresh set is `in_play_clause`, which
drops a film the day it opens, and no other pass fetched `/movie/{id}` for it. The history this
writes is the same `catalog.film_release_date_change` the full upsert writes, through the same
`rebuild_release_dates`, so the sweep's `release_events` phase cards it on its next run exactly
as it cards an in-play film's moves (D-1532.5) — one carder, one dedup rule, one place for the
rule that a date already past when observed is not news (D-1532.6).

**The released half of the provider poll's set** (`providers.load_poll_half`, D-1532.3): rule-1
films past their theatrical date, plus title-followed films that have opened. The video poll
reads the other half, so the providers run still costs each film two reads a day.

**It reads release dates and writes release dates, nothing else** (D-1532.4). No credits, no
fields, no status — a full upsert of a released film would diff its cast and studios too, and
put exactly the cards this ticket exists to stop back on the feed.

**First observation is a baseline, never a change** (ADR-0014), by the rebuild's own rule on
`film.release_dates_observed_at`. In practice every film here was observed before it opened, so
a baseline is rare — a film imported already released is the one case.

A phase of the providers run, on `run_video_poll`'s contract: one session per film, progress
against the run, a consecutive-failure abort of its own, and a 404 tombstoned rather than
counted as a failure.
"""

import logging
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.ingest.providers import load_poll_half
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.client import TMDBClient, TMDBNotFound
from upmovies.ingest.tmdb.upsert import mark_film_missing, rebuild_release_dates

log = logging.getLogger(__name__)


@dataclass
class ReleaseDatesResult:
    """What one release-date poll selected, read and wrote."""

    selected: int = 0
    polled: int = 0
    changes: int = 0
    """`film_release_date_change` rows written — what the sweep will card from. Spikes once
    after deploy, when every released film's slate is read for the first time since it opened,
    and sits near zero after."""
    baselined: int = 0
    """Films whose slate this pass observed for the first time: rows written, nothing recorded
    (ADR-0014)."""
    missing: int = 0
    """Films TMDB answered 404 for, and this pass tombstoned."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


async def _release_dates_observed(session: AsyncSession, film_id: UUID) -> bool:
    """Whether the catalog had observed this film's release slate before this read — asked
    before the rebuild sets the marker, only so the pass can count its baselines."""
    observed_at = (
        await session.execute(select(Film.release_dates_observed_at).where(Film.id == film_id))
    ).scalar_one_or_none()
    return observed_at is not None


async def run_release_date_poll(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    today: date,
    min_age_days: int,
    max_age_days: int,
    excluded_statuses: frozenset[str],
    failure_threshold: int = 10,
    log_every: int = 250,
) -> ReleaseDatesResult:
    """Read `/movie/{id}/release_dates` for every released film in the scoped set, one at a
    time, and rebuild its stored slate."""
    result = ReleaseDatesResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        targets = await load_poll_half(
            s,
            released=True,
            today=today,
            min_age_days=min_age_days,
            max_age_days=max_age_days,
            excluded_statuses=excluded_statuses,
        )
    result.selected = len(targets)
    log.info("release dates: %d films due", result.selected)

    for i, target in enumerate(targets, start=1):
        await heartbeat.tick()
        try:
            payload = await client.movie_release_dates(target.tmdb_id)
            async with owned_session(session_factory) as s:
                observed_before = await _release_dates_observed(s, target.film_id)
                changes = await rebuild_release_dates(s, target.film_id, payload)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            result.polled += 1
            result.changes += changes
            result.baselined += 0 if observed_before else 1
            guard.succeeded()
            if i % log_every == 0:
                log.info("release dates: %d/%d films", i, len(targets))
            continue
        except TMDBNotFound:
            # Terminal, not an outage — tombstoned rather than retried, and it touches `guard`
            # in neither direction, for the reasons `refresh_phase` gives at the same call.
            # Rarely reached: the provider poll runs first over the whole set and tombstones
            # there, and `poll_set_clause` drops a tombstoned film.
            async with owned_session(session_factory) as s:
                await mark_film_missing(s, target.tmdb_id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            result.missing += 1
            log.info("release dates: film %d is gone from TMDB (404); tombstoned", target.tmdb_id)
            continue
        except httpx.HTTPError as e:
            log.warning("polling release dates for film %d failed: %s", target.tmdb_id, e)
        except Exception:
            # One malformed payload must not cost the rest of the poll.
            log.exception("unexpected error polling release dates for film %d", target.tmdb_id)
        result.failures += 1
        if await guard.failed():
            result.aborted = True
            result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
            log.error("release dates: %s", result.abort_error)
            break

    log.info(
        "release dates: %d polled, %d changes, %d baselined, %d missing, %d failed",
        result.polled,
        result.changes,
        result.baselined,
        result.missing,
        result.failures,
    )
    return result


def release_dates_detail(result: ReleaseDatesResult) -> str:
    """The release-date phase's clause of the run's `ingest_run.detail` line.

    `changes` rather than `carded`: this pass cards nothing itself — the sweep does, from these
    rows, on its next run (D-1532.5) — so the number to compare against is the sweep's
    `release dates:` clause the morning after.
    """
    line = (
        f"release dates: {result.polled}/{result.selected} polled, "
        f"{result.changes} changes, {result.baselined} baselined, "
        f"{result.missing} missing, {result.failures} failed"
    )
    if result.aborted:
        line += f"; release dates aborted: {result.abort_error}"
    return line
