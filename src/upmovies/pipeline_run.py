"""In-process ingestion orchestration for the Coolify scheduled tasks.

Each stage runner builds its own client + session, runs one pipeline to completion, and
always finalizes its run (→ `failed` on an unexpected crash) — the same contract the
`/admin/ingest/*` trigger endpoints rely on, so `routers.ingest_admin` imports these
rather than duplicating the wiring.

`run_sweep` is deliberately **not** one of `run_daily`'s stages (spec §6.1, ADR-0013): the
daily chain is fail-fast, so a TMDB hiccup partway through a ~45-minute sweep would abort
feeds, link *and* synthesize for the day. It runs on its own Coolify slot roughly two hours
ahead of the daily one, so films it admits are in the retrieval index for that day's link
pass, and carries its own deadman URL — a sweep that stops running would otherwise be
invisible in the daily check's ping.

`run_daily` / `run_hourly` run the stages **sequentially in one process**: because each
stage is awaited to completion before the next begins, `synthesize` cannot start until
`link` has fully finished — there is no HTTP poll window to time out. The daily chain is
fail-fast: the first stage that does not reach `succeeded` aborts the rest. A best-effort
healthchecks.io deadman ping (`/start` at the top, base URL on success, `/fail` on any
failure) drives alerting.

`run_providers` is a fourth slot on the same terms as the sweep: the D-27 watch-provider poll
reads a working set the sweep has already dropped (films past their theatrical release), makes
no model calls, and carries its own deadman so a poll that stops running does not hide behind a
green sweep.

Entry point: `python -m upmovies.pipeline_run {daily|hourly|sweep|providers}`.
"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select

from upmovies.config import Settings, get_settings
from upmovies.db import SessionLocal
from upmovies.ingest.models import IngestRun
from upmovies.ingest.providers import providers_detail, run_provider_poll
from upmovies.ingest.runs import create_run, finalize_run, mark_stale_runs_cancelled
from upmovies.ingest.sweep import (
    AdmissionTranches,
    CreditDetachmentResult,
    CreditEventResult,
    DerivationResult,
    EnumerateResult,
    FieldEventResult,
    RefreshResult,
    ReleaseEventResult,
    run_credit_attachment_events,
    run_credit_detachment_events,
    run_field_change_events,
    run_release_date_events,
    run_sweep_enumerate,
    run_sweep_refresh,
    run_watchlist_derivation,
    sweep_detail,
    validate_sweep_configuration,
)
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.service import run_tmdb_ingest
from upmovies.link.pipeline import run_link_ingest
from upmovies.link.resolve.scoring import Thresholds
from upmovies.llm import Gateway, validate_stage_configuration
from upmovies.logging_config import configure_logging
from upmovies.mail import validate_mail_configuration
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
        async with TMDBClient.from_settings(settings) as client:
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


@asynccontextmanager
async def _resolve_client(settings: Settings) -> AsyncIterator[TMDBClient | None]:
    """The TMDB client person resolution reads `/search/person` through, or None when
    `RESOLVE_ENABLED` is off.

    A context manager yielding None rather than a flag threaded into `run_link_ingest`: with
    the switch off no client should be *opened* either, and the one place that can honour
    that is the one place that would open it."""
    if not settings.resolve_enabled:
        yield None
        return
    async with TMDBClient.from_settings(settings) as client:
        yield client


async def run_link_stage(run_id: UUID, settings: Settings) -> None:
    try:
        # One gateway, four stages: `link`, `source_judge`, `cluster` and `resolve` all run
        # inside `run_link_ingest` and each resolves its own provider from it. This used to be
        # one `AnthropicClient` opened here and threaded down to all three (NEU-980, spec §5.3).
        #
        # The TMDB client is the fourth pass's and not the `resolve` stage's: person resolution
        # (D-21) is deterministic Python over `/search/person`, and it runs here rather than
        # on its own Coolify slot because it reads what clustering wrote a moment earlier. The
        # model it does reach for — the closed-set tiebreak (D-22) — answers only the narrow
        # band that arithmetic could not separate, which is why it is a stage on the gateway
        # above rather than a second client opened beside the TMDB one.
        # `RESOLVE_ENABLED=false` hands `run_link_ingest` no client at all, which is what
        # makes the switch a genuine skip rather than a pass that runs and discards its work.
        async with Gateway(settings) as gateway, _resolve_client(settings) as tmdb_client:
            await run_link_ingest(
                session_factory=_session_factory,
                gateway=gateway,
                run_id=run_id,
                model=settings.link_model,
                cluster_model=settings.cluster_model,
                recency_days=settings.link_recency_days,
                attach_limit=settings.link_cluster_attach_limit,
                batch_size=settings.link_batch_size,
                floor=settings.link_confidence_floor,
                cluster_max_tokens=settings.link_cluster_max_tokens,
                source_gate_enabled=settings.source_gate_enabled,
                source_judge_model=settings.source_judge_model,
                unresolved_tier=settings.source_unresolved_tier,
                dedup_days=settings.link_singular_dedup_days,
                release_change_window_days=settings.link_release_change_window_days,
                story_confirm_days=settings.sweep_story_confirm_days,
                retrieval_threshold=settings.link_retrieval_threshold,
                retrieval_max_candidates=settings.link_retrieval_max_candidates,
                retrieval_max_zero_candidate_rate=settings.link_retrieval_max_zero_candidate_rate,
                retrieval_health_min_stories=settings.link_retrieval_health_min_stories,
                retrieval_saturation_warn_rate=settings.link_retrieval_saturation_warn_rate,
                tmdb_client=tmdb_client,
                resolve_thresholds=Thresholds(
                    accept_floor=settings.resolve_accept_floor,
                    accept_margin=settings.resolve_accept_margin,
                ),
                resolve_mentions_per_run=settings.resolve_mentions_per_run,
                resolve_model=settings.resolve_model,
            )
    except Exception as e:
        log.exception("link ingest crashed")
        await _finalize_failed(run_id, str(e))


async def run_synthesize_stage(run_id: UUID, settings: Settings) -> None:
    try:
        async with Gateway(settings) as gateway:
            await run_synthesize_ingest(
                session_factory=_session_factory,
                gateway=gateway,
                run_id=run_id,
                model=settings.summary_model,
                prompt_version=settings.summary_prompt_version,
                url_resolve_per_run=settings.url_resolve_per_run,
                url_resolve_max_attempts=settings.url_resolve_max_attempts,
                url_resolve_delay_seconds=settings.url_resolve_delay_seconds,
            )
    except Exception as e:
        log.exception("synthesize ingest crashed")
        await _finalize_failed(run_id, str(e))


async def run_sweep_stage(run_id: UUID, settings: Settings) -> None:
    """Every sweep phase against one run row: enumerate, refresh, card the field changes and
    the credit attachments refreshing produced, derive the watchlist items the new credits
    qualify, then the run's terminal status.

    The odd one out among the stage runners: the phases it calls do **not** finalize. They
    share a single `ingest_run` row — one sweep, one row on `/admin/runs` — so the status,
    the error, and the detail line carrying every phase's counters are written here (§6.2).
    """
    try:
        today = date.today()
        now = datetime.now(UTC)
        async with TMDBClient.from_settings(settings) as client:
            enumerated = await run_sweep_enumerate(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                today=today,
                excluded_statuses=settings.tmdb_excluded_statuses,
                dormancy_days=settings.sweep_dormancy_days,
                corroboration_threshold=settings.sweep_corroboration_threshold,
                tranches=AdmissionTranches.from_settings(settings),
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
            # Unconditional, even when enumerate gave up. Refresh is the phase the whole
            # catalog-sourced-event feature rests on (§6.2), and skipping it on an enumerate
            # abort would mean a TMDB flake in the last few of ~7,500 people silently costs
            # the catalog a day of movement. Its own consecutive-failure guard bounds what a
            # real outage costs to find out: `INGEST_CONSECUTIVE_FAILURE_THRESHOLD` requests.
            refreshed = await run_sweep_refresh(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                today=today,
                excluded_statuses=settings.tmdb_excluded_statuses,
                dormancy_days=settings.sweep_dormancy_days,
                dormant_refresh_days=settings.sweep_dormant_refresh_days,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
        # Outside the TMDB client: this phase reads the changes the two above wrote and makes
        # no HTTP calls. Unconditional for the same reason refresh is — a refresh whose
        # changes are never read produces exactly as much as no refresh at all — and it runs
        # last so this pass's own upserts are already in `film_field_change` (ADR-0014).
        carded = await run_field_change_events(
            session_factory=_session_factory,
            run_id=run_id,
            now=now,
            lookback_days=settings.sweep_event_lookback_days,
            failure_threshold=settings.ingest_consecutive_failure_threshold,
        )
        # The credit half, on the same terms and the same window as the field half — and
        # separately, because the two read different tables and a failure in one says nothing
        # about the other. Last, for the same reason: `_rebuild_joins` writes
        # `film_credit_change` during refresh, so this pass's own attachments are in hand.
        #
        # The one carding phase that takes a TMDB client, and so the one inside a client of
        # its own: the sanity holds (D-8) read `/person/{id}` for birth and death dates. It is
        # a second `async with` rather than an extension of the one above because the phase
        # order is load-bearing — this must run after `run_field_change_events`, which reads
        # the changes refresh wrote — and the process-wide limiter means the two clients share
        # one TMDB budget regardless (NEU-1399). Lazily, once per person, and only for people
        # about to be carded, so most passes make no request here at all.
        async with TMDBClient.from_settings(settings) as client:
            attached = await run_credit_attachment_events(
                session_factory=_session_factory,
                run_id=run_id,
                now=now,
                lookback_days=settings.sweep_event_lookback_days,
                quarantine_hours=settings.sweep_credit_quarantine_hours,
                story_confirm_days=settings.sweep_story_confirm_days,
                client=client,
                max_films_per_day=settings.sweep_sanity_max_films_per_day,
                posthumous_years=settings.sweep_sanity_posthumous_years,
                min_age_years=settings.sweep_sanity_min_age_years,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
        # The detachment half (NEU-1200), on the same terms and the same window as the
        # attachment half — and separately, because the two read the same table with
        # different filters and a failure in one says nothing about the other.
        detached = await run_credit_detachment_events(
            session_factory=_session_factory,
            run_id=run_id,
            now=now,
            lookback_days=settings.sweep_event_lookback_days,
            dwell_days=settings.sweep_credit_dwell_days,
            failure_threshold=settings.ingest_consecutive_failure_threshold,
        )
        # The release-date half (NEU-1121), reading `film_release_date_change` — which the
        # refresh above has just written, same as the credit half. Its own phase because its
        # source table is its own: the field half reads the `catalog.film` trigger and no
        # longer touches release dates at all.
        released = await run_release_date_events(
            session_factory=_session_factory,
            run_id=run_id,
            now=now,
            lookback_days=settings.sweep_event_lookback_days,
            corroboration_window_days=settings.link_release_change_window_days,
            failure_threshold=settings.ingest_consecutive_failure_threshold,
        )
        # D-13's maintenance half (NEU-1352), and the only phase that writes nothing to the
        # catalog or the feed: it reads `catalog.film_credit` as refresh has just rebuilt it and
        # adds the watchlist items those credits newly qualify. Last, because it depends on that
        # rebuild and on nothing the carding phases do — a film is derivable whether or not its
        # attachment carded — and because a failure here must not cost the day's events.
        derived = await run_watchlist_derivation(
            session_factory=_session_factory,
            run_id=run_id,
            today=today,
            excluded_statuses=settings.tmdb_excluded_statuses,
            failure_threshold=settings.ingest_consecutive_failure_threshold,
        )
        # Inside the `try` deliberately: a stage runner that lets an exception escape leaves
        # the run `running` and skips the deadman's `/fail`, so the write that finalizes has
        # to be covered by the same net as the work it reports on.
        await _finalize_sweep(
            run_id, enumerated, refreshed, carded, attached, detached, released, derived
        )
    except Exception as e:
        log.exception("sweep crashed")
        await _finalize_failed(run_id, str(e))


async def _finalize_sweep(
    run_id: UUID,
    enumerated: EnumerateResult,
    refreshed: RefreshResult,
    carded: FieldEventResult,
    attached: CreditEventResult,
    detached: CreditDetachmentResult,
    released: ReleaseEventResult,
    derived: DerivationResult,
) -> None:
    """Write the sweep's terminal status: `failed` iff a phase gave up on consecutive
    failures, and the every-phase detail line either way — a run that aborted still reports
    what it managed to do before it did."""
    aborts = [
        f"{phase} phase {result.abort_error}"
        for phase, result in (
            ("enumerate", enumerated),
            ("refresh", refreshed),
            ("events", carded),
            ("credits", attached),
            ("credit removals", detached),
            ("release dates", released),
            ("watchlist", derived),
        )
        if result.aborted
    ]
    async with SessionLocal() as s:
        await finalize_run(
            s,
            run_id,
            status="failed" if aborts else "succeeded",
            error="; ".join(aborts) or None,
            detail=sweep_detail(
                enumerated, refreshed, carded, attached, detached, released, derived
            ),
        )
        await s.commit()


async def run_providers_stage(run_id: UUID, settings: Settings) -> None:
    """The watch-provider poll against one run row (D-27): read the scoped set, write the
    first-seen ledger and the current-availability snapshot, then the run's terminal status.

    One phase, so unlike the sweep there is nothing to sequence — but the same division of
    labour: the phase does not finalize, because the status, the error and the detail line
    belong to whoever opened the run (§6.2).
    """
    try:
        async with TMDBClient.from_settings(settings) as client:
            polled = await run_provider_poll(
                session_factory=_session_factory,
                client=client,
                run_id=run_id,
                today=date.today(),
                min_age_days=settings.provider_poll_min_age_days,
                max_age_days=settings.provider_poll_max_age_days,
                failure_threshold=settings.ingest_consecutive_failure_threshold,
            )
        # Inside the `try`, for the reason `run_sweep_stage` gives: the write that finalizes
        # has to be covered by the same net as the work it reports on.
        async with SessionLocal() as s:
            await finalize_run(
                s,
                run_id,
                status="failed" if polled.aborted else "succeeded",
                error=polled.abort_error,
                detail=providers_detail(polled),
            )
            await s.commit()
    except Exception as e:
        log.exception("provider poll crashed")
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


# The modes that reach no model and send no mail, and so are exempted from the LLM-routing and
# mail guards in `main` — see the comment there. Membership is a property of what a mode *does*,
# not of its schedule: `providers` joins the sweep because the D-27 poll is TMDB reads and
# catalog writes end to end (NEU-1374). M7's `notify` and `digest` will not join it.
_NO_MODEL_CALL_MODES = frozenset({"sweep", "providers"})

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


async def _clear_stale_runs(settings: Settings) -> None:
    """Cancel runs orphaned by a crash, a restart, or a killed scheduled task, before this
    task opens its own.

    The app's lifespan runs the same cleanup, but under Coolify that fires only on deploy —
    so before this, a run orphaned at 05:00 sat `running` until the next release. On
    2026-08-11 one had to be cancelled by hand. Running it from every scheduled task means
    the hourly slot clears an orphan within the hour (NEU-1117).

    Safe to run from a task that may overlap a live sweep only because expiry is
    heartbeat-based: a sweep in its silent enumerate stretch is still ticking
    `last_progress_at`, so it is not an orphan. Best-effort — a cleanup that fails must not
    cost the pipeline its run.
    """
    try:
        async with SessionLocal() as s:
            cancelled = await mark_stale_runs_cancelled(
                s, stale_after_minutes=settings.ingest_stale_run_minutes
            )
            await s.commit()
        if cancelled:
            log.warning("cancelled %d stale run(s) before starting", cancelled)
    except Exception:
        log.exception("stale-run cleanup failed; continuing")


async def run_daily(settings: Settings) -> bool:
    """Run the full daily chain sequentially, fail-fast. Returns True iff every stage
    succeeded. Pings the daily deadman check at start / success / failure."""
    await _clear_stale_runs(settings)
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
    await _clear_stale_runs(settings)
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


async def run_sweep(settings: Settings) -> bool:
    """Run the undated-film sweep on its own run kind. Returns True iff it succeeded.
    Pings the sweep deadman check at start / success / failure."""
    await _clear_stale_runs(settings)
    await _ping(settings.healthcheck_sweep_url, "/start")
    status = await _run_tracked_stage("sweep", lambda rid, s: run_sweep_stage(rid, s), settings)
    if status != "succeeded":
        log.error("sweep pipeline failed: ended %s", status)
        await _ping(settings.healthcheck_sweep_url, "/fail")
        return False
    log.info("sweep pipeline succeeded")
    await _ping(settings.healthcheck_sweep_url)
    return True


async def run_providers(settings: Settings) -> bool:
    """Run the watch-provider poll on its own run kind. Returns True iff it succeeded.
    Pings the providers deadman check at start / success / failure."""
    await _clear_stale_runs(settings)
    await _ping(settings.healthcheck_providers_url, "/start")
    status = await _run_tracked_stage(
        "providers", lambda rid, s: run_providers_stage(rid, s), settings
    )
    if status != "succeeded":
        log.error("provider poll failed: ended %s", status)
        await _ping(settings.healthcheck_providers_url, "/fail")
        return False
    log.info("provider poll succeeded")
    await _ping(settings.healthcheck_providers_url)
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "daily"
    settings = get_settings()
    configure_logging(settings.log_level)
    # The same guard the app's lifespan runs, for the process that actually pays for the
    # failure: a scheduled task is a separate `python -m upmovies.pipeline_run` invocation,
    # so the app having booted proves nothing about the env this one was handed (AGENTS.md's
    # "a long-running container holds the env it was created with"). Checked before the first
    # run row exists, so an unroutable stage costs no half-published run (NEU-981).
    # Except for the modes that make no model calls: failing one on an unrelated LLM
    # routing typo would re-introduce exactly the shared failure mode §6.1 keeps them out of
    # the daily chain to avoid, and it would surface only as deadman silence.
    # The mail configuration rides on the same guard, and inherits that exemption for the
    # same reason: those modes are deliberately outside the daily chain's shared failure modes
    # (§6.1), and failing one on a setting it never reads would put it back inside them. No
    # mode sends mail *today* — the check is here because M7's notify and digest passes are
    # `pipeline_run` modes, and they should find the guard already in place rather than
    # discover a missing RESEND_API_KEY partway through a digest run.
    if mode not in _NO_MODEL_CALL_MODES:
        validate_stage_configuration(settings)
        validate_mail_configuration(settings)
    # Unconditional, and so deliberately outside the exemption above: this one is the sweep's
    # *own* configuration (NEU-1368). The exemption exists so an unrelated LLM or mail typo
    # cannot fail the one mode that is kept out of the daily chain's shared failure modes —
    # it is not a general licence for the sweep to start unchecked, and a quarantine window
    # wider than the rolling lookback is a sweep that silently stops carding attachments.
    #
    # Here and **not** in the app's lifespan, unlike the three guards above it. The API never
    # runs the sweep, so refusing its boot over this would trade the website for a batch
    # setting it does not read. This process is the one that pays, and `hourly` runs it every
    # hour — so a bad Coolify value surfaces within the hour as a failed task and a
    # healthchecks.io `/fail`, which is the alerting path, with the site still up.
    validate_sweep_configuration(settings)
    if mode == "daily":
        ok = asyncio.run(run_daily(settings))
    elif mode == "hourly":
        ok = asyncio.run(run_hourly(settings))
    elif mode == "sweep":
        ok = asyncio.run(run_sweep(settings))
    elif mode == "providers":
        ok = asyncio.run(run_providers(settings))
    else:
        print(
            f"unknown mode {mode!r}: expected 'daily', 'hourly', 'sweep' or 'providers'",
            file=sys.stderr,
        )
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
