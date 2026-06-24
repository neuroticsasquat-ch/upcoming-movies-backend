import pytest
from pydantic import ValidationError

from upmovies.config import Settings

_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://a:b@c:5432/d",
    "ADMIN_TOKEN": "xxx",
    "TMDB_API_KEY": "tmdb-xxx",
    "ANTHROPIC_API_KEY": "anthropic-xxx",
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
        "TMDB_EXCLUDED_STATUSES",
        "INGEST_CONSECUTIVE_FAILURE_THRESHOLD",
        "INGEST_STALE_RUN_MINUTES",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_release_window_past_days == 0
    assert s.tmdb_release_window_future_days == 1095
    assert s.tmdb_min_popularity == 1.0
    assert s.tmdb_excluded_statuses == frozenset({"Released", "Canceled"})
    assert s.ingest_consecutive_failure_threshold == 10
    assert s.ingest_stale_run_minutes == 15


def test_settings_excluded_statuses_parsed_and_overridable(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("TMDB_EXCLUDED_STATUSES", "Released, Canceled , Rumored")
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_excluded_statuses == frozenset({"Released", "Canceled", "Rumored"})


def test_settings_ingestion_overrides_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("TMDB_MIN_POPULARITY", "2.5")
    monkeypatch.setenv("TMDB_RELEASE_WINDOW_FUTURE_DAYS", "365")
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_min_popularity == 2.5
    assert s.tmdb_release_window_future_days == 365


def test_settings_link_use_batches_defaults_true(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("LINK_USE_BATCHES", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.link_use_batches is True


def test_settings_link_use_batches_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_USE_BATCHES", "true")
    s = Settings()  # type: ignore[call-arg]
    assert s.link_use_batches is True


def test_settings_link_batch_size_default(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("LINK_BATCH_SIZE", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.link_batch_size == 15


def test_settings_link_batch_size_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_BATCH_SIZE", "10")
    s = Settings()  # type: ignore[call-arg]
    assert s.link_batch_size == 10


def test_settings_cluster_use_batches_defaults_true(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("CLUSTER_USE_BATCHES", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.cluster_use_batches is True


def test_settings_cluster_use_batches_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("CLUSTER_USE_BATCHES", "false")
    s = Settings()  # type: ignore[call-arg]
    assert s.cluster_use_batches is False


def test_settings_summary_defaults(monkeypatch):
    _set_required(monkeypatch)
    for key in ("SUMMARY_MODEL", "SUMMARY_USE_BATCHES", "SUMMARY_PROMPT_VERSION"):
        monkeypatch.delenv(key, raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.summary_model == "claude-haiku-4-5"
    assert s.summary_use_batches is True
    assert s.summary_prompt_version == "3"


def test_settings_summary_overrides_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("SUMMARY_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("SUMMARY_USE_BATCHES", "false")
    monkeypatch.setenv("SUMMARY_PROMPT_VERSION", "2")
    s = Settings()  # type: ignore[call-arg]
    assert s.summary_model == "claude-sonnet-4-6"
    assert s.summary_use_batches is False
    assert s.summary_prompt_version == "2"


def test_settings_requires_tmdb_api_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _REQUIRED_ENV["DATABASE_URL"])
    monkeypatch.setenv("ADMIN_TOKEN", _REQUIRED_ENV["ADMIN_TOKEN"])
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
