"""In-process ingestion orchestration for the Coolify scheduled tasks.

Each stage runner builds its own client + session, runs one pipeline to completion, and
always finalizes its run (→ `failed` on an unexpected crash) — the same contract the
`/admin/ingest/*` trigger endpoints rely on, so `routers.ingest_admin` imports these
rather than duplicating the wiring.

`run_daily` / `run_hourly` run the stages **sequentially in one process**: because each
stage is awaited to completion before the next begins, `synthesize` cannot start until
`link` has fully finished — there is no HTTP poll window to time out, so slow Anthropic
Message Batches no longer fail the pipeline. The daily chain is fail-fast: the first stage
that does not reach `succeeded` aborts the rest. A best-effort healthchecks.io deadman ping
(`/start` at the top, base URL on success, `/fail` on any failure) drives alerting.

Entry point: `python -m upmovies.pipeline_run {daily|hourly}`.
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select

from upmovies.config import Settings, get_settings
from upmovies.db import SessionLocal
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run, finalize_run
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.service import run_tmdb_ingest
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm.client import AnthropicClient
from upmovies.news.fetcher import run_feeds_ingest
from upmovies.synthesize.pipeline import run_synthesize_ingest

log = logging.getLogger(__name__)

# A stage runner: given (run_id, settings), run one pipeline to completion and finalize it.
StageRunner = Callable[[UUID, Settings], Awaitable[None]]


def _session_factory():
    return SessionLocal()


async def _finalize_failed(run_id: UUID, error: str) -> None:
    async with SessionLocal() as s:
        await finalize_run(s, run_id, status="failed", error=error)
        await s.commit()


async def run_tmdb_stage(run_id: UUID, settings: Settings) -> None:
    try:
        today = date.today()
        async with TMDBClient(
            base_url=settings.tmdb_base_url,
            api_key=settings.tmdb_api_key,
            rate_calls=settings.tmdb_rate_limit_requests,
            rate_window=settings.tmdb_rate_limit_window_seconds,
            retry_max_attempts=settings.tmdb_retry_max_attempts,
        ) as client:
            await run_tmdb_ingest(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                release_date_gte=today - timedelta(days=settings.tmdb_release_window_past_days),
                release_date_lte=today + timedelta(days=settings.tmdb_release_window_future_days),
                min_popularity=settings.tmdb_min_popularity,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
                excluded_statuses=settings.tmdb_excluded_statuses,
                min_runtime=settings.tmdb_min_runtime,
            )
    except Exception as e:
        log.exception("tmdb ingest crashed")
        await _finalize_failed(run_id, str(e))


async def run_feeds_stage(
    run_id: UUID, settings: Settings, per_film_override: bool | None = None
) -> None:
    try:
        await run_feeds_ingest(
            session_factory=_session_factory,
            run_id=run_id,
            recency_days=settings.feed_recency_days,
            google_enabled=settings.news_google_enabled,
            per_film_enabled=per_film_override
            if per_film_override is not None
            else settings.feeds_per_film_enabled,
            per_film_throttle=settings.feeds_per_film_throttle_seconds,
            per_film_title_filter_enabled=settings.per_film_title_filter_enabled,
            per_film_title_match_min_ratio=settings.per_film_title_match_min_ratio,
        )
    except Exception as e:
        log.exception("feeds ingest crashed")
        await _finalize_failed(run_id, str(e))


async def run_link_stage(run_id: UUID, settings: Settings) -> None:
    try:
        async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
            await run_link_ingest(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                model=settings.link_model,
                cluster_model=settings.cluster_model,
                recency_days=settings.link_recency_days,
                attach_limit=settings.link_cluster_attach_limit,
                batch_size=settings.link_batch_size,
                floor=settings.link_confidence_floor,
                use_batches=settings.link_use_batches,
                cluster_use_batches=settings.cluster_use_batches,
                cluster_max_tokens=settings.link_cluster_max_tokens,
                source_gate_enabled=settings.source_gate_enabled,
                source_judge_model=settings.source_judge_model,
                unresolved_tier=settings.source_unresolved_tier,
                dedup_days=settings.link_singular_dedup_days,
                release_change_window_days=settings.link_release_change_window_days,
            )
    except Exception as e:
        log.exception("link ingest crashed")
        await _finalize_failed(run_id, str(e))


async def run_synthesize_stage(run_id: UUID, settings: Settings) -> None:
    try:
        async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
            await run_synthesize_ingest(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                model=settings.summary_model,
                prompt_version=settings.summary_prompt_version,
                use_batches=settings.summary_use_batches,
                url_resolve_per_run=settings.url_resolve_per_run,
                url_resolve_max_attempts=settings.url_resolve_max_attempts,
                url_resolve_delay_seconds=settings.url_resolve_delay_seconds,
            )
    except Exception as e:
        log.exception("synthesize ingest crashed")
        await _finalize_failed(run_id, str(e))


async def _run_tracked_stage(kind: str, runner: StageRunner, settings: Settings) -> str:
    """Open a run of `kind`, execute `runner` to completion (it finalizes its own run), and
    return the run's terminal status (`succeeded` / `failed` / `cancelled`)."""
    async with SessionLocal() as s:
        run_id = await create_run(s, kind=kind)
        await s.commit()
    await runner(run_id, settings)
    async with SessionLocal() as s:
        status = (
            await s.execute(select(IngestRun.status).where(IngestRun.id == run_id))
        ).scalar_one()
    log.info("%s run %s finished: %s", kind, run_id, status)
    return status


async def _ping(base_url: str | None, suffix: str = "") -> None:
    """Best-effort healthchecks.io ping. No-op when `base_url` is unset; a ping failure is
    logged and swallowed so it never affects the pipeline's own outcome."""
    if not base_url:
        return
    url = base_url.rstrip("/") + suffix
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url)
    except Exception:
        log.warning("healthcheck ping to %s failed", url, exc_info=True)


# Daily chain: TMDB refresh → per-film feed pass → LLM link/cluster → summarize. `feeds`
# forces per_film=true; the light per_film=false pass runs hourly (run_hourly).
# Lambdas (not bare references) so each runner is resolved from module globals at call time
# — keeps the sequence uniformly monkeypatchable and lets `feeds` pin per_film=true.
_DAILY_STAGES: list[tuple[str, StageRunner]] = [
    ("tmdb", lambda rid, s: run_tmdb_stage(rid, s)),
    ("feeds", lambda rid, s: run_feeds_stage(rid, s, per_film_override=True)),
    ("link", lambda rid, s: run_link_stage(rid, s)),
    ("synthesize", lambda rid, s: run_synthesize_stage(rid, s)),
]


async def run_daily(settings: Settings) -> bool:
    """Run the full daily chain sequentially, fail-fast. Returns True iff every stage
    succeeded. Pings the daily deadman check at start / success / failure."""
    await _ping(settings.healthcheck_daily_url, "/start")
    for kind, runner in _DAILY_STAGES:
        status = await _run_tracked_stage(kind, runner, settings)
        if status != "succeeded":
            log.error("daily pipeline aborting: %s stage ended %s", kind, status)
            await _ping(settings.healthcheck_daily_url, "/fail")
            return False
    log.info("daily pipeline succeeded")
    await _ping(settings.healthcheck_daily_url)
    return True


async def run_hourly(settings: Settings) -> bool:
    """Run the light hourly feeds pass (per_film=false). Returns True iff it succeeded.
    Pings the hourly deadman check at start / success / failure."""
    await _ping(settings.healthcheck_hourly_url, "/start")
    status = await _run_tracked_stage(
        "feeds", lambda rid, s: run_feeds_stage(rid, s, per_film_override=False), settings
    )
    if status != "succeeded":
        log.error("hourly feeds pipeline failed: ended %s", status)
        await _ping(settings.healthcheck_hourly_url, "/fail")
        return False
    log.info("hourly feeds pipeline succeeded")
    await _ping(settings.healthcheck_hourly_url)
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "daily"
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
    if mode == "daily":
        ok = asyncio.run(run_daily(settings))
    elif mode == "hourly":
        ok = asyncio.run(run_hourly(settings))
    else:
        print(f"unknown mode {mode!r}: expected 'daily' or 'hourly'", file=sys.stderr)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
