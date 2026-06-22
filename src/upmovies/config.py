from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    tmdb_excluded_statuses_raw: str = Field(
        default="Released,Canceled", alias="TMDB_EXCLUDED_STATUSES"
    )

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    link_model: str = Field(default="claude-haiku-4-5", alias="LINK_MODEL")
    cluster_model: str = Field(default="claude-sonnet-4-6", alias="CLUSTER_MODEL")
    link_confidence_floor: float = Field(default=0.7, alias="LINK_CONFIDENCE_FLOOR")
    link_recency_days: int = Field(default=4, alias="LINK_RECENCY_DAYS")
    link_batch_size: int = Field(default=15, alias="LINK_BATCH_SIZE")
    link_use_batches: bool = Field(default=True, alias="LINK_USE_BATCHES")
    summary_model: str = Field(default="claude-haiku-4-5", alias="SUMMARY_MODEL")
    summary_use_batches: bool = Field(default=True, alias="SUMMARY_USE_BATCHES")
    summary_prompt_version: str = Field(default="1", alias="SUMMARY_PROMPT_VERSION")
    feed_recency_days: int = Field(default=3, alias="FEED_RECENCY_DAYS")
    feeds_per_film_enabled: bool = Field(default=True, alias="FEEDS_PER_FILM_ENABLED")
    feeds_per_film_throttle_seconds: float = Field(
        default=1.0, alias="FEEDS_PER_FILM_THROTTLE_SECONDS"
    )

    ingest_consecutive_failure_threshold: int = Field(
        default=10, alias="INGEST_CONSECUTIVE_FAILURE_THRESHOLD"
    )
    ingest_stale_run_minutes: int = Field(default=15, alias="INGEST_STALE_RUN_MINUTES")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    cors_allowed_origins_raw: str = Field(
        default="https://app.upmovies.localhost", alias="CORS_ALLOWED_ORIGINS"
    )

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
