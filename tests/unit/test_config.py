from typing import get_args

import pytest
from pydantic import ValidationError

from upmovies.config import Provider, Settings
from upmovies.link.retrieval import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_SCORE_THRESHOLD,
    MAX_ZERO_CANDIDATE_RATE,
    MIN_STORIES_FOR_BREACH,
)
from upmovies.llm.registry import PROVIDERS

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
    assert s.ingest_stale_run_minutes == 30


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


def test_settings_link_batch_size_default(monkeypatch):
    """20, re-derived at NEU-1001 — the reply ceiling sets it now, not a cached prefix."""
    _set_required(monkeypatch)
    monkeypatch.delenv("LINK_BATCH_SIZE", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.link_batch_size == 20


def test_settings_link_batch_size_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_BATCH_SIZE", "10")
    s = Settings()  # type: ignore[call-arg]
    assert s.link_batch_size == 10


def test_settings_summary_defaults(monkeypatch):
    _set_required(monkeypatch)
    for key in ("SUMMARY_MODEL", "SUMMARY_PROMPT_VERSION"):
        monkeypatch.delenv(key, raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.summary_model == "claude-haiku-4-5"
    assert s.summary_prompt_version == "9"


def test_settings_summary_overrides_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("SUMMARY_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("SUMMARY_PROMPT_VERSION", "2")
    s = Settings()  # type: ignore[call-arg]
    assert s.summary_model == "claude-sonnet-4-6"
    assert s.summary_prompt_version == "2"


_PROVIDER_ENV = (
    "LINK_PROVIDER",
    "CLUSTER_PROVIDER",
    "SOURCE_JUDGE_PROVIDER",
    "SUMMARY_PROVIDER",
    "DEEPINFRA_API_KEY",
    "DEEPSEEK_API_KEY",
)


def _clear_providers(monkeypatch):
    for key in _PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)


def test_settings_defaults_every_stage_to_anthropic(monkeypatch):
    """Default config must reproduce today's behaviour exactly — the gateway's exit criterion
    is capability, and nothing migrates (design §1)."""
    _set_required(monkeypatch)
    _clear_providers(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    assert s.link_provider == "anthropic"
    assert s.cluster_provider == "anthropic"
    assert s.source_judge_provider == "anthropic"
    assert s.summary_provider == "anthropic"


def test_settings_reads_per_stage_providers_from_env(monkeypatch):
    _set_required(monkeypatch)
    _clear_providers(monkeypatch)
    monkeypatch.setenv("CLUSTER_PROVIDER", "deepinfra")
    monkeypatch.setenv("SUMMARY_PROVIDER", "deepseek")
    s = Settings()  # type: ignore[call-arg]
    assert s.cluster_provider == "deepinfra"
    assert s.summary_provider == "deepseek"
    assert s.link_provider == "anthropic"  # untouched stages stay put


@pytest.mark.parametrize(
    "key", ["LINK_PROVIDER", "CLUSTER_PROVIDER", "SOURCE_JUDGE_PROVIDER", "SUMMARY_PROVIDER"]
)
def test_settings_rejects_an_unknown_provider(monkeypatch, key):
    """What the `Literal` buys: a typo fails the container at boot rather than the stage at
    its first call, halfway through a nightly publish."""
    _set_required(monkeypatch)
    _clear_providers(monkeypatch)
    monkeypatch.setenv(key, "anthropik")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_provider_literal_matches_the_registry():
    """`Provider` restates `llm.registry.PROVIDERS` because `config` cannot import the `llm`
    package — `llm.gateway` reads `Settings`, so the import would be circular. Same bind as
    the retrieval constants, same pinning test."""
    assert set(get_args(Provider)) == set(PROVIDERS)


def test_settings_provider_credentials_are_optional(monkeypatch):
    """Adding these as required fields would break every deploy that does not use them
    (design §8) — which today is all of them."""
    _set_required(monkeypatch)
    _clear_providers(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    assert s.deepinfra_api_key is None
    assert s.deepseek_api_key is None


def test_settings_reads_provider_credentials_from_env(monkeypatch):
    _set_required(monkeypatch)
    _clear_providers(monkeypatch)
    monkeypatch.setenv("DEEPINFRA_API_KEY", "di-xxx")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-xxx")
    s = Settings()  # type: ignore[call-arg]
    assert s.deepinfra_api_key == "di-xxx"
    assert s.deepseek_api_key == "ds-xxx"


def test_settings_requires_tmdb_api_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _REQUIRED_ENV["DATABASE_URL"])
    monkeypatch.setenv("ADMIN_TOKEN", _REQUIRED_ENV["ADMIN_TOKEN"])
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_per_film_title_filter_defaults(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("PER_FILM_TITLE_FILTER_ENABLED", raising=False)
    monkeypatch.delenv("PER_FILM_TITLE_MATCH_MIN_RATIO", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.per_film_title_filter_enabled is True
    assert s.per_film_title_match_min_ratio == 0.4


def test_settings_per_film_title_match_min_ratio_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PER_FILM_TITLE_MATCH_MIN_RATIO", "0.6")
    s = Settings()  # type: ignore[call-arg]
    assert s.per_film_title_match_min_ratio == 0.6


def test_settings_public_base_url_default(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.public_base_url == "http://localhost:5173"


def test_settings_public_base_url_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://backlotter.com")
    s = Settings()  # type: ignore[call-arg]
    assert s.public_base_url == "https://backlotter.com"


def test_settings_min_runtime_default(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("TMDB_MIN_RUNTIME", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_min_runtime == 60


def test_settings_min_runtime_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("TMDB_MIN_RUNTIME", "0")
    s = Settings()  # type: ignore[call-arg]
    assert s.tmdb_min_runtime == 0


def test_settings_has_url_resolve_defaults(monkeypatch):
    _set_required(monkeypatch)
    for key in (
        "URL_RESOLVE_PER_RUN",
        "URL_RESOLVE_MAX_ATTEMPTS",
        "URL_RESOLVE_DELAY_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.url_resolve_per_run == 500
    assert s.url_resolve_max_attempts == 3
    assert s.url_resolve_delay_seconds == 1.0


def test_settings_reads_url_resolve_overrides(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("URL_RESOLVE_PER_RUN", "120")
    monkeypatch.setenv("URL_RESOLVE_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("URL_RESOLVE_DELAY_SECONDS", "0.25")
    s = Settings()  # type: ignore[call-arg]
    assert s.url_resolve_per_run == 120
    assert s.url_resolve_max_attempts == 5
    assert s.url_resolve_delay_seconds == 0.25


def test_settings_link_singular_dedup_days_default(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("LINK_SINGULAR_DEDUP_DAYS", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.link_singular_dedup_days == 14


def test_settings_link_singular_dedup_days_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_SINGULAR_DEDUP_DAYS", "7")
    s = Settings()  # type: ignore[call-arg]
    assert s.link_singular_dedup_days == 7


def test_settings_link_release_change_window_days_default(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.delenv("LINK_RELEASE_CHANGE_WINDOW_DAYS", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.link_release_change_window_days == 14


def test_settings_link_release_change_window_days_override_from_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RELEASE_CHANGE_WINDOW_DAYS", "3")
    s = Settings()  # type: ignore[call-arg]
    assert s.link_release_change_window_days == 3


_RETRIEVAL_ENV = (
    "LINK_RETRIEVAL_THRESHOLD",
    "LINK_RETRIEVAL_MAX_CANDIDATES",
    "LINK_RETRIEVAL_MAX_ZERO_CANDIDATE_RATE",
    "LINK_RETRIEVAL_HEALTH_MIN_STORIES",
)


def _clear_retrieval(monkeypatch):
    for key in _RETRIEVAL_ENV:
        monkeypatch.delenv(key, raising=False)


def test_settings_link_retrieval_tuning_defaults(monkeypatch):
    """Both settings default, so no local or test run needs new env (NEU-995)."""
    _set_required(monkeypatch)
    _clear_retrieval(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    # Tuned at NEU-1001 against 98,662 real stories and a 1,226-film catalog: T=0.5 is the
    # top of the flat region (every reachable roster pick scores 0.5 or better) and K=25
    # clears both the p99 candidate set of 18 and the deepest pick seen, at rank 21.
    assert s.link_retrieval_threshold == 0.5
    assert s.link_retrieval_max_candidates == 25


def test_settings_link_retrieval_defaults_match_the_selector(monkeypatch):
    """The tuning defaults must not drift from `link.retrieval.select`'s own.

    They are duplicated rather than shared because `config` cannot import the selector:
    `retrieval/index.py` reads settings, so the import would be circular. This pins them.
    """
    _set_required(monkeypatch)
    _clear_retrieval(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    assert s.link_retrieval_threshold == DEFAULT_SCORE_THRESHOLD
    assert s.link_retrieval_max_candidates == DEFAULT_CANDIDATE_LIMIT


def test_settings_link_retrieval_tuning_overrides_from_env(monkeypatch):
    """T and K move by config, not by deploy — which is what made NEU-1001 a config change,
    and what lets the next catalog expansion be answered the same way."""
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RETRIEVAL_THRESHOLD", "0.34")
    monkeypatch.setenv("LINK_RETRIEVAL_MAX_CANDIDATES", "10")
    s = Settings()  # type: ignore[call-arg]
    assert s.link_retrieval_threshold == 0.34
    assert s.link_retrieval_max_candidates == 10


@pytest.mark.parametrize("threshold", ["-0.1", "1.5"])
def test_settings_link_retrieval_threshold_rejects_out_of_range(monkeypatch, threshold):
    """Scores are token fractions, so a threshold outside 0..1 can only mean a mistake —
    above 1.0 nothing ever clears it and every story becomes a zero-candidate reject."""
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RETRIEVAL_THRESHOLD", threshold)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_link_retrieval_max_candidates_rejects_zero(monkeypatch):
    """A cap of zero offers the model nothing while retrieval still reports a hit —
    a story that is neither linked nor a zero-candidate reject."""
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RETRIEVAL_MAX_CANDIDATES", "0")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_retrieval_health_guard_defaults_match_the_rule(monkeypatch):
    """The hard-breach constants, duplicated into config for the same reason T and K are —
    `config` cannot import `link.retrieval` without a cycle — and pinned here (NEU-1002)."""
    _set_required(monkeypatch)
    _clear_retrieval(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    assert s.link_retrieval_max_zero_candidate_rate == MAX_ZERO_CANDIDATE_RATE
    assert s.link_retrieval_health_min_stories == MIN_STORIES_FOR_BREACH


def test_settings_retrieval_health_guard_overrides_from_env(monkeypatch):
    """A guard that can only be retuned by a deploy is one that gets switched off instead —
    and 1.0 is how it *is* switched off, no rate being above it."""
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RETRIEVAL_MAX_ZERO_CANDIDATE_RATE", "1.0")
    monkeypatch.setenv("LINK_RETRIEVAL_HEALTH_MIN_STORIES", "200")
    s = Settings()  # type: ignore[call-arg]
    assert s.link_retrieval_max_zero_candidate_rate == 1.0
    assert s.link_retrieval_health_min_stories == 200


@pytest.mark.parametrize("rate", ["-0.1", "1.5"])
def test_settings_retrieval_max_zero_candidate_rate_rejects_out_of_range(monkeypatch, rate):
    """It is a share of the run's stories, so anything outside 0..1 can only be a mistake —
    and above 1.0 the guard is unreachable rather than merely lenient."""
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RETRIEVAL_MAX_ZERO_CANDIDATE_RATE", rate)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_retrieval_health_min_stories_rejects_negative(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RETRIEVAL_HEALTH_MIN_STORIES", "-1")
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_settings_sweep_admission_is_off_by_default(monkeypatch):
    """A sweep that admits nothing is the shipped configuration (spec §7.4): the master
    switch and all three tranches are off until a ramp ticket opens one."""
    _set_required(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    assert s.sweep_enabled is False
    assert (s.sweep_admit_directors, s.sweep_admit_writers, s.sweep_admit_cast) == (
        False,
        False,
        False,
    )


def test_settings_reads_sweep_admission_flags_from_env(monkeypatch):
    """Opening a tranche is an env change, not a deploy — which is what makes the ramp
    reversible in one move."""
    _set_required(monkeypatch)
    monkeypatch.setenv("SWEEP_ENABLED", "true")
    monkeypatch.setenv("SWEEP_ADMIT_DIRECTORS", "true")
    s = Settings()  # type: ignore[call-arg]
    assert s.sweep_enabled is True
    assert s.sweep_admit_directors is True
    assert (s.sweep_admit_writers, s.sweep_admit_cast) == (False, False)
