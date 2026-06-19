"""TMDB discover ingestion: enumerate films in a rolling release-date window, upsert
the canonical `catalog.film` spine, and track progress via the ingest_run table.

Discover is used purely to enumerate candidate tmdb_ids (gated on popularity); the full
record — including the status/imdb_id that discover omits — comes from a per-film details
fetch. The caller supplies the window/filter bounds and a session factory so the service
stays decoupled from Settings and easy to drive in tests."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.ingest.runs import finalize_run, record_progress
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.upsert import upsert_film

log = logging.getLogger(__name__)

# TMDB caps the discover endpoint at 500 pages regardless of total_pages.
_MAX_DISCOVER_PAGES = 500

SessionFactory = Callable[[], AsyncSession]


@dataclass
class IngestResult:
    films_processed: int
    films_failed: int
    films_skipped: int = 0


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


async def _discover_candidate_ids(
    *,
    client: TMDBClient,
    release_date_gte: date,
    release_date_lte: date,
    min_popularity: float,
) -> list[int]:
    """Page through discover (popularity-desc) collecting tmdb_ids until a film falls
    below the popularity floor — since results are sorted, everything after it does too."""
    candidates: list[int] = []
    page = 1
    while True:
        resp = await client.discover_movies(
            page=page,
            sort_by="popularity.desc",
            **{
                "primary_release_date.gte": release_date_gte.isoformat(),
                "primary_release_date.lte": release_date_lte.isoformat(),
            },
        )
        below_floor = False
        for movie in resp.results:
            if movie.popularity is None or movie.popularity < min_popularity:
                below_floor = True
                break
            candidates.append(movie.id)
        if below_floor or page >= resp.total_pages or page >= _MAX_DISCOVER_PAGES:
            break
        page += 1
    return candidates


async def run_tmdb_ingest(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    release_date_gte: date,
    release_date_lte: date,
    min_popularity: float,
    failure_threshold: int = 10,
    excluded_statuses: frozenset[str] = frozenset(),
) -> IngestResult:
    candidate_ids = await _discover_candidate_ids(
        client=client,
        release_date_gte=release_date_gte,
        release_date_lte=release_date_lte,
        min_popularity=min_popularity,
    )

    processed = 0
    failed = 0
    skipped = 0
    consecutive_failures = 0

    for tmdb_id in candidate_ids:
        try:
            details = await client.movie_details(tmdb_id)
            if details.status in excluded_statuses:
                skipped += 1
                consecutive_failures = 0
                continue
            async with _owned_session(session_factory) as s:
                await upsert_film(s, details)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            processed += 1
            consecutive_failures = 0
            continue
        except httpx.HTTPStatusError as e:
            log.warning("skipping film %d after http error: %s", tmdb_id, e)
        except Exception:
            log.exception("unexpected error ingesting film %d", tmdb_id)

        failed += 1
        consecutive_failures += 1
        async with _owned_session(session_factory) as s:
            await record_progress(s, run_id, failed_delta=1)
            await s.commit()
        if consecutive_failures >= failure_threshold:
            async with _owned_session(session_factory) as s:
                await finalize_run(
                    s,
                    run_id,
                    status="failed",
                    error=f"aborted after {consecutive_failures} consecutive failures",
                )
                await s.commit()
            return IngestResult(processed, failed, skipped)

    async with _owned_session(session_factory) as s:
        await finalize_run(
            s,
            run_id,
            status="succeeded",
            detail=f"upserted {processed}, skipped {skipped} (excluded status)",
        )
        await s.commit()
    return IngestResult(processed, failed, skipped)
