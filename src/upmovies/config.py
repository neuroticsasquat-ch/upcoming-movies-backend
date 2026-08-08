from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The candidate-retrieval rollout states (NEU-995). Three rather than a boolean because the
# middle one is where the cutover evidence comes from: retrieval is pure and free, so it can
# run beside the incumbent on live traffic at no added LLM cost.
#
#   off     roster path only — current behaviour.
#   shadow  the roster path still decides; retrieval runs beside it and records what it
#           *would* have offered. No behaviour change.
#   on      retrieval decides; the roster path is not built.
LinkRetrievalMode = Literal["off", "shadow", "on"]


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

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    link_model: str = Field(default="claude-haiku-4-5", alias="LINK_MODEL")
    cluster_model: str = Field(default="claude-sonnet-4-6", alias="CLUSTER_MODEL")
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
    # Defaults to `off`, so no local or test run needs new env to keep current behaviour.
    link_retrieval_mode: LinkRetrievalMode = Field(default="off", alias="LINK_RETRIEVAL_MODE")
    # T and K for `link.retrieval.select`, tuned at NEU-1001 over 98,662 real stories
    # against the 1,226-film production catalog (design spec §5.2). They stay settings so
    # the next catalog expansion is answered by config rather than by a deploy. They mirror
    # the selector's own module defaults; `config` cannot import those (retrieval/index.py
    # reads settings, so it would be a cycle), so a test pins the two together instead.
    link_retrieval_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, alias="LINK_RETRIEVAL_THRESHOLD"
    )
    link_retrieval_max_candidates: int = Field(
        default=25, ge=1, alias="LINK_RETRIEVAL_MAX_CANDIDATES"
    )
    # The hard-breach guard (NEU-1002, ADR-0010): a zero-candidate rate above the ceiling
    # finalizes the run `failed`, which aborts the daily chain and pings the deadman. Mirrors
    # `link.retrieval.health`'s own constants — same duplication, same pinning test, same
    # reason — and both stay settings so an incident is answered from env: 1.0 disarms the
    # guard, and the minimum denominator is what stops a quiet news day tripping it.
    link_retrieval_max_zero_candidate_rate: float = Field(
        default=0.25, ge=0.0, le=1.0, alias="LINK_RETRIEVAL_MAX_ZERO_CANDIDATE_RATE"
    )
    link_retrieval_health_min_stories: int = Field(
        default=50, ge=0, alias="LINK_RETRIEVAL_HEALTH_MIN_STORIES"
    )
    source_gate_enabled: bool = Field(default=True, alias="SOURCE_GATE_ENABLED")
    source_judge_model: str = Field(default="claude-haiku-4-5", alias="SOURCE_JUDGE_MODEL")
    source_unresolved_tier: str = Field(default="acceptable", alias="SOURCE_UNRESOLVED_TIER")
    summary_model: str = Field(default="claude-haiku-4-5", alias="SUMMARY_MODEL")
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
    ingest_stale_run_minutes: int = Field(default=15, alias="INGEST_STALE_RUN_MINUTES")

    # healthchecks.io deadman ping URLs for the Coolify scheduled tasks (see
    # upmovies.pipeline_run). Optional: unset → the ping is a no-op, so local/dev runs of
    # `python -m upmovies.pipeline_run` don't need them. `daily` runs the full chain
    # (tmdb → feeds → link → synthesize); `hourly` runs the light feeds-only pass.
    healthcheck_daily_url: str | None = Field(default=None, alias="HEALTHCHECK_DAILY_URL")
    healthcheck_hourly_url: str | None = Field(default=None, alias="HEALTHCHECK_HOURLY_URL")

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
