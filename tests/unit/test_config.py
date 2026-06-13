import pytest
from pydantic import ValidationError

from upmovies.config import Settings

_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://a:b@c:5432/d",
    "ADMIN_TOKEN": "xxx",
    "TMDB_API_KEY": "tmdb-xxx",
}


def _set_required(monkeypatch):
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_settings_reads_tmdb_api_key_from_env(monkeypatch):
    _set_required(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_api_key == "tmdb-xxx"


def test_settings_has_sensible_tmdb_defaults(monkeypatch):
    _set_required(monkeypatch)
    for key in (
        "TMDB_BASE_URL",
        "TMDB_RATE_LIMIT_REQUESTS",
        "TMDB_RATE_LIMIT_WINDOW_SECONDS",
        "TMDB_RETRY_MAX_ATTEMPTS",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_base_url == "https://api.themoviedb.org/3"
    assert s.tmdb_rate_limit_requests == 40
    assert s.tmdb_rate_limit_window_seconds == 10
    assert s.tmdb_retry_max_attempts == 5


def test_settings_has_sensible_ingestion_defaults(monkeypatch):
    _set_required(monkeypatch)
    for key in (
        "TMDB_RELEASE_WINDOW_PAST_DAYS",
        "TMDB_RELEASE_WINDOW_FUTURE_DAYS",
        "TMDB_MIN_POPULARITY",
        "TMDB_REGION",
        "TMDB_ORIGINAL_LANGUAGE",
        "INGEST_CONSECUTIVE_FAILURE_THRESHOLD",
        "INGEST_STALE_RUN_MINUTES",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_release_window_past_days == 30
    assert s.tmdb_release_window_future_days == 1095
    assert s.tmdb_min_popularity == 10.0
    assert s.tmdb_region == "US"
    assert s.tmdb_original_language == "en"
    assert s.ingest_consecutive_failure_threshold == 10
    assert s.ingest_stale_run_minutes == 15


def test_settings_ingestion_overrides_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("TMDB_MIN_POPULARITY", "2.5")
    monkeypatch.setenv("TMDB_REGION", "GB")
    monkeypatch.setenv("TMDB_RELEASE_WINDOW_FUTURE_DAYS", "365")
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_min_popularity == 2.5
    assert s.tmdb_region == "GB"
    assert s.tmdb_release_window_future_days == 365


def test_settings_requires_tmdb_api_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _REQUIRED_ENV["DATABASE_URL"])
    monkeypatch.setenv("ADMIN_TOKEN", _REQUIRED_ENV["ADMIN_TOKEN"])
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
