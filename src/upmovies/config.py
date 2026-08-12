from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The host a stage's model is served by — the gateway's second axis, alongside the per-stage
# model settings that were already here (design §8). A `Literal` rather than a bare `str` so a
# misspelled provider fails the container at boot rather than the stage at its first call: the
# same discipline `rates_for` applies to an unknown `(provider, model)`.
#
# The names are duplicated from `llm.registry.PROVIDERS` rather than imported. `llm.gateway`
# reads `Settings`, so importing the `llm` package here would be a cycle — the same bind the
# retrieval constants below are in, and a test pins the two together the same way.
Provider = Literal["anthropic", "deepinfra", "deepseek"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    test_database_url: str | None = Field(default=None, alias="TEST_DATABASE_URL")
    admin_token: str = Field(..., alias="ADMIN_TOKEN")

    tmdb_api_key: str = Field(..., alias="TMDB_API_KEY")
    tmdb_base_url: str = Field(default="https://api.themoviedb.org/3", alias="TMDB_BASE_URL")
    tmdb_rate_limit_requests: int = Field(default=40, alias="TMDB_RATE_LIMIT_REQUESTS")
    tmdb_rate_limit_window_seconds: int = Field(default=10, alias="TMDB_RATE_LIMIT_WINDOW_SECONDS")
    tmdb_retry_max_attempts: int = Field(default=5, alias="TMDB_RETRY_MAX_ATTEMPTS")

    # Rolling release-date window + filters for the TMDB discover ingestion.
    tmdb_release_window_past_days: int = Field(default=0, alias="TMDB_RELEASE_WINDOW_PAST_DAYS")
    tmdb_release_window_future_days: int = Field(
        default=1095, alias="TMDB_RELEASE_WINDOW_FUTURE_DAYS"
    )
    tmdb_min_popularity: float = Field(default=1.0, alias="TMDB_MIN_POPULARITY")
    tmdb_min_runtime: int = Field(default=60, alias="TMDB_MIN_RUNTIME")
    tmdb_excluded_statuses_raw: str = Field(
        default="Released,Canceled", alias="TMDB_EXCLUDED_STATUSES"
    )

    # How long an undated film may stay quiescent — no `catalog.film_field_change` row and
    # no linked story — before `active_film_clause` drops it from the working set (ADR-0015).
    # Placeholder: deliberately long, so almost nothing goes dormant until the M4 tuning
    # ticket sets it from the discovery probe. Erring long costs per-film queries; erring
    # short silently stops following live films.
    sweep_dormancy_days: int = Field(default=365, ge=1, alias="SWEEP_DORMANCY_DAYS")
    # How often the sweep's refresh phase still re-fetches a dormant film. Dormancy is not
    # an exemption from refreshing — detecting the change that revives a film requires
    # reading it, so a dormancy that stopped the refresh would be a one-way door (§4.5) —
    # it is a *reduced cadence*. Placeholder until the M4 tuning ticket sets it: erring
    # short only costs requests, erring long delays every revival by that much.
    sweep_dormant_refresh_days: int = Field(default=30, ge=1, alias="SWEEP_DORMANT_REFRESH_DAYS")
    # How far back the sweep's field-change phase reads `catalog.film_field_change` for events
    # to card (ADR-0014). A fixed rolling window, not a watermark: re-reading a carded change
    # is a no-op, so the overlap costs a couple of indexed queries and means a failed sweep
    # loses nothing. It is also the day-one guard — the table holds months of history, and
    # without a floor the first pass after deploy would card every date move ever recorded.
    sweep_event_lookback_days: int = Field(default=7, ge=1, alias="SWEEP_EVENT_LOOKBACK_DAYS")

    # The sweep's master switch, in the manner of NEWS_GOOGLE_ENABLED: off means it still
    # enumerates and still reports, but writes nothing (spec §7.3). Kept separate from the
    # three tranche flags below so a rollback is one move and does not disturb the ramp.
    sweep_enabled: bool = Field(default=False, alias="SWEEP_ENABLED")
    # Admission ramps one seed grade at a time — directors, then writers, then top-5 cast
    # (§7.4) — so the retrieval-health guard reacts to a 1,446-person expansion before a
    # 7,519-person one, and a precision drop names the grade that caused it. All off until
    # the M4 tranche tickets open them, which makes each one an env change, not a deploy.
    sweep_admit_directors: bool = Field(default=False, alias="SWEEP_ADMIT_DIRECTORS")
    sweep_admit_writers: bool = Field(default=False, alias="SWEEP_ADMIT_WRITERS")
    sweep_admit_cast: bool = Field(default=False, alias="SWEEP_ADMIT_CAST")
    # How many distinct seed people must reach an undated film before it may be admitted
    # (§4.1). One director attachment is the earliest and most valuable signal the product
    # sells; it is also exactly what a speculative TMDB entry looks like, and §4.2 left that
    # tension open for measurement rather than taste.
    #
    # **Measured 2026-08-11 (NEU-1087); 1 is no longer a placeholder.** The probe ran against
    # a snapshot taken minutes before the directors tranche opened — the pre-expansion
    # distribution, which cannot be retaken — and found 639 director-reached candidates of
    # 3,250. Raising the bar to 2 cuts that by 60% while the `Rumored` share, the only
    # available signature of a speculative entry, does not move: 19.4% -> 19.6%. Of the 384
    # films it would drop, 19.3% are `Rumored`, indistinguishable from the base rate — so 2
    # does not select against vaporware, it selects against being early, which is the signal
    # the product sells. It would also discard 113 films already `In Production` or `Post
    # Production` to remove 74 `Rumored` ones. Only at 3 does the `Rumored` share fall, on a
    # tranche of 87. Full table in spec §4.3.
    #
    # Ground truth is still owed: "was it real" properly means what fraction later went
    # dormant, and nothing can go dormant until 2027 at the current N (NEU-1118). Status is
    # a proxy. If that figure ever contradicts this, it wins.
    sweep_corroboration_threshold: int = Field(
        default=1, ge=1, alias="SWEEP_CORROBORATION_THRESHOLD"
    )

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    # Optional, deliberately unlike ANTHROPIC_API_KEY above: every deploy today is Anthropic
    # for all four stages, and requiring these would break every one of them for a capability
    # none of them uses (design §8). Boot-time validation (NEU-981) is what makes optional
    # safe — it asserts a credential exists for each *configured* provider, at startup.
    deepinfra_api_key: str | None = Field(default=None, alias="DEEPINFRA_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    link_model: str = Field(default="claude-haiku-4-5", alias="LINK_MODEL")
    link_provider: Provider = Field(default="anthropic", alias="LINK_PROVIDER")
    cluster_model: str = Field(default="claude-sonnet-4-6", alias="CLUSTER_MODEL")
    cluster_provider: Provider = Field(default="anthropic", alias="CLUSTER_PROVIDER")
    link_confidence_floor: float = Field(default=0.7, alias="LINK_CONFIDENCE_FLOOR")
    link_recency_days: int = Field(default=4, alias="LINK_RECENCY_DAYS")
    # Re-derived at NEU-1001. The old 15 was chosen when a ~46k-token roster prefix was
    # cached and amortized across the batch; the retrieval path sends no prefix, so what
    # bounds the batch now is the **reply**: `_MAX_TOKENS` caps it at 2048, and a batch
    # whose reply is truncated fails to parse and takes every story in it down. At 20 the
    # worst-case reply measures 1,183 tok (58% of the ceiling) while the instruction block
    # falls to 11.4% of the request, against 14.7% at 15. A batch of 40 overruns the reply
    # ceiling outright.
    link_batch_size: int = Field(default=20, alias="LINK_BATCH_SIZE")
    link_cluster_max_tokens: int = Field(default=4096, alias="LINK_CLUSTER_MAX_TOKENS")
    link_cluster_attach_limit: int = Field(default=25, alias="LINK_CLUSTER_ATTACH_LIMIT")
    link_singular_dedup_days: int = Field(default=14, alias="LINK_SINGULAR_DEDUP_DAYS")
    link_release_change_window_days: int = Field(
        default=14, alias="LINK_RELEASE_CHANGE_WINDOW_DAYS"
    )
    # T and K for `link.retrieval.select`, re-derived at NEU-1088 over the post-directors-
    # tranche catalog (1,997 active films) — see that module's docstring and design spec
    # §5.13. They stay settings so the *next* catalog expansion is answered by config rather
    # than by a deploy, which is expected: the cast tranche (NEU-1090) roughly doubles the
    # catalog again. They mirror the selector's own module defaults; `config` cannot import
    # those (retrieval/index.py reads settings, so it would be a cycle), so a test pins the
    # two together instead.
    link_retrieval_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, alias="LINK_RETRIEVAL_THRESHOLD"
    )
    link_retrieval_max_candidates: int = Field(
        default=35, ge=1, alias="LINK_RETRIEVAL_MAX_CANDIDATES"
    )
    # The hard-breach guard (NEU-1002, ADR-0010): a zero-candidate rate above the ceiling
    # finalizes the run `failed`, which aborts the daily chain and pings the deadman. Mirrors
    # `link.retrieval.health`'s own constants — same duplication, same pinning test, same
    # reason — and both stay settings so an incident is answered from env: 1.0 disarms the
    # guard, and the minimum denominator is what stops a quiet news day tripping it.
    #
    # Tightened 0.25 → 0.10 at NEU-1088. The ceiling has to stay *below* what a mis-set T
    # would produce or it cannot catch the failure ADR-0010 names, and the gap had gone
    # slack: zero-candidate falls as the catalog grows, so T=0.6's rate fell from 32.6% to
    # 25.6% and left 0.25 clearing it by 0.6pp (spec §5.13).
    link_retrieval_max_zero_candidate_rate: float = Field(
        default=0.10, ge=0.0, le=1.0, alias="LINK_RETRIEVAL_MAX_ZERO_CANDIDATE_RATE"
    )
    link_retrieval_health_min_stories: int = Field(
        default=50, ge=0, alias="LINK_RETRIEVAL_HEALTH_MIN_STORIES"
    )
    # The soft tier (NEU-1088 §3.6): a cap-saturation rate above this flags the health row
    # and names itself in the run's detail line. It does **not** fail the run — saturation is
    # drift, and `run_daily` is fail-fast. Provisional: calibrated on one post-flip run plus
    # one offline grid, and the cast tranche is expected to move it.
    link_retrieval_saturation_warn_rate: float = Field(
        default=0.05, ge=0.0, le=1.0, alias="LINK_RETRIEVAL_SATURATION_WARN_RATE"
    )
    source_gate_enabled: bool = Field(default=True, alias="SOURCE_GATE_ENABLED")
    source_judge_model: str = Field(default="claude-haiku-4-5", alias="SOURCE_JUDGE_MODEL")
    source_judge_provider: Provider = Field(default="anthropic", alias="SOURCE_JUDGE_PROVIDER")
    source_unresolved_tier: str = Field(default="acceptable", alias="SOURCE_UNRESOLVED_TIER")
    summary_model: str = Field(default="claude-haiku-4-5", alias="SUMMARY_MODEL")
    # Named for the setting beside it, not for the stage: the stage is `summarize`, and
    # `Gateway` owns that one-line mapping rather than renaming a live env var.
    summary_provider: Provider = Field(default="anthropic", alias="SUMMARY_PROVIDER")
    summary_prompt_version: str = Field(default="9", alias="SUMMARY_PROMPT_VERSION")
    url_resolve_per_run: int = Field(default=500, alias="URL_RESOLVE_PER_RUN")
    url_resolve_max_attempts: int = Field(default=3, alias="URL_RESOLVE_MAX_ATTEMPTS")
    url_resolve_delay_seconds: float = Field(default=1.0, alias="URL_RESOLVE_DELAY_SECONDS")
    feed_recency_days: int = Field(default=3, alias="FEED_RECENCY_DAYS")
    # NEU-717 master gate: when off, no Google News at all (broad queries + per-film),
    # regardless of feeds_per_film_enabled. Paused by default on a trial basis.
    news_google_enabled: bool = Field(default=False, alias="NEWS_GOOGLE_ENABLED")
    feeds_per_film_enabled: bool = Field(default=True, alias="FEEDS_PER_FILM_ENABLED")
    feeds_per_film_throttle_seconds: float = Field(
        default=1.0, alias="FEEDS_PER_FILM_THROTTLE_SECONDS"
    )
    per_film_title_filter_enabled: bool = Field(default=True, alias="PER_FILM_TITLE_FILTER_ENABLED")
    per_film_title_match_min_ratio: float = Field(
        default=0.4, alias="PER_FILM_TITLE_MATCH_MIN_RATIO"
    )

    ingest_consecutive_failure_threshold: int = Field(
        default=10, alias="INGEST_CONSECUTIVE_FAILURE_THRESHOLD"
    )
    # How long a `running` run may go without a heartbeat before startup/scheduled-task
    # cleanup cancels it. Read against `last_progress_at`, not `started_at` (NEU-1117), so
    # this bounds *silence*, not total runtime: ~5x the longest legitimate gap anywhere
    # (one retrying LLM batch) and 30x the sweep's heartbeat interval. It was 15 when it
    # meant runtime, which cancelled live multi-hour sweeps on any restart.
    ingest_stale_run_minutes: int = Field(default=30, alias="INGEST_STALE_RUN_MINUTES")

    # healthchecks.io deadman ping URLs for the Coolify scheduled tasks (see
    # upmovies.pipeline_run). Optional: unset → the ping is a no-op, so local/dev runs of
    # `python -m upmovies.pipeline_run` don't need them. `daily` runs the full chain
    # (tmdb → feeds → link → synthesize); `hourly` runs the light feeds-only pass; `sweep`
    # runs the undated-film pass on its own slot ~2h ahead of daily, with its own deadman
    # because a sweep that stops running is invisible in the daily chain's ping (§6.1).
    healthcheck_daily_url: str | None = Field(default=None, alias="HEALTHCHECK_DAILY_URL")
    healthcheck_hourly_url: str | None = Field(default=None, alias="HEALTHCHECK_HOURLY_URL")
    healthcheck_sweep_url: str | None = Field(default=None, alias="HEALTHCHECK_SWEEP_URL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    cors_allowed_origins_raw: str = Field(
        default="https://app.upmovies.localhost", alias="CORS_ALLOWED_ORIGINS"
    )
    public_base_url: str = Field(default="http://localhost:5173", alias="PUBLIC_BASE_URL")

    session_cookie_name: str = Field(default="upmovies_session", alias="SESSION_COOKIE_NAME")
    csrf_cookie_name: str = Field(default="csrf_token", alias="CSRF_COOKIE_NAME")
    session_ttl_days: int = Field(default=30, alias="SESSION_TTL_DAYS")
    cookie_secure: bool = Field(default=True, alias="COOKIE_SECURE")
    cookie_samesite: str = Field(default="lax", alias="COOKIE_SAMESITE")
    cookie_domain: str | None = Field(default=None, alias="COOKIE_DOMAIN")

    login_lockout_threshold: int = Field(default=5, alias="LOGIN_LOCKOUT_THRESHOLD")
    login_lockout_window_minutes: int = Field(default=15, alias="LOGIN_LOCKOUT_WINDOW_MINUTES")

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins_raw.split(",") if o.strip()]

    @property
    def tmdb_excluded_statuses(self) -> frozenset[str]:
        return frozenset(s.strip() for s in self.tmdb_excluded_statuses_raw.split(",") if s.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
