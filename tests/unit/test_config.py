import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from upmovies.config import Provider, Settings
from upmovies.link.retrieval import (
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_SCORE_THRESHOLD,
    MAX_ZERO_CANDIDATE_RATE,
    MIN_STORIES_FOR_BREACH,
    SATURATION_WARN_RATE,
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
    "LINK_RETRIEVAL_SATURATION_WARN_RATE",
)


def _clear_retrieval(monkeypatch):
    for key in _RETRIEVAL_ENV:
        monkeypatch.delenv(key, raising=False)


def test_settings_link_retrieval_tuning_defaults(monkeypatch):
    """Both settings default, so no local or test run needs new env (NEU-995)."""
    _set_required(monkeypatch)
    _clear_retrieval(monkeypatch)
    s = Settings()  # type: ignore[call-arg]
    # Re-derived at NEU-1088 over the post-directors-tranche catalog (1,997 active films):
    # T=0.5 still tops the flat region — recall is identical from 0.25 to 0.5 and falls at
    # 0.6 — and K=35 is the deepest pick seen, at rank 31, plus a named margin of 4.
    assert s.link_retrieval_threshold == 0.5
    assert s.link_retrieval_max_candidates == 35


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
    assert s.link_retrieval_saturation_warn_rate == SATURATION_WARN_RATE


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


def test_settings_retrieval_saturation_warn_rate_overrides_from_env(monkeypatch):
    """The soft tier is retunable from env for the same reason the hard one is, and 1.0 is
    how it is switched off — no rate being above it. Expected to be exercised: the threshold
    is provisional, calibrated on one post-flip run plus one offline grid (NEU-1088 §3.6)."""
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RETRIEVAL_SATURATION_WARN_RATE", "0.2")
    s = Settings()  # type: ignore[call-arg]
    assert s.link_retrieval_saturation_warn_rate == 0.2


@pytest.mark.parametrize("rate", ["-0.1", "1.5"])
def test_settings_retrieval_saturation_warn_rate_rejects_out_of_range(monkeypatch, rate):
    """A share of the run's stories, same as the zero-candidate ceiling beside it."""
    _set_required(monkeypatch)
    monkeypatch.setenv("LINK_RETRIEVAL_SATURATION_WARN_RATE", rate)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


# --- prod compose drift (NEU-1088) ---------------------------------------------------

_PROD_COMPOSE = Path(__file__).parents[2] / "docker-compose.prod.yml"

# Every retrieval setting `docker-compose.prod.yml` passes through, against the `Settings`
# field it feeds. Production reads its environment from that file, so a fallback left behind
# here does not merely disagree with the code — it *wins over* it, silently.
_PINNED_PROD_FALLBACKS = (
    ("LINK_RETRIEVAL_THRESHOLD", "link_retrieval_threshold"),
    ("LINK_RETRIEVAL_MAX_CANDIDATES", "link_retrieval_max_candidates"),
    ("LINK_RETRIEVAL_MAX_ZERO_CANDIDATE_RATE", "link_retrieval_max_zero_candidate_rate"),
    ("LINK_RETRIEVAL_HEALTH_MIN_STORIES", "link_retrieval_health_min_stories"),
    ("LINK_RETRIEVAL_SATURATION_WARN_RATE", "link_retrieval_saturation_warn_rate"),
)


def _compose_fallback(env_name: str) -> str:
    """The `VALUE` out of a `NAME: "${NAME:-VALUE}"` line in the prod compose file.

    Read with a regex rather than a YAML parse: what is being checked is the *interpolation
    default* inside the string, which no YAML loader resolves — to a parser the value is
    opaquely `${NAME:-VALUE}`, so parsing would buy nothing and cost a dependency."""
    match = re.search(
        rf'^\s*{env_name}:\s*"\$\{{{env_name}:-(?P<value>[^}}]*)\}}"\s*$',
        _PROD_COMPOSE.read_text(),
        re.MULTILINE,
    )
    assert match is not None, f"{env_name} is not passed through docker-compose.prod.yml"
    return match.group("value")


@pytest.mark.skipif(
    not _PROD_COMPOSE.exists(),
    reason=(
        "docker-compose.prod.yml is not visible from inside the dev container — CI runs "
        "pytest against the checkout, where it is, and is the enforcement point"
    ),
)
@pytest.mark.parametrize(("env_name", "field"), _PINNED_PROD_FALLBACKS)
def test_prod_compose_fallbacks_match_the_code_defaults(monkeypatch, env_name, field):
    """A stale fallback in `docker-compose.prod.yml` silently overrides a retune.

    This is not hypothetical. NEU-1088 moved K from 25 to 35 and the zero-candidate ceiling
    from 0.25 to 0.10, and production kept running the old pair — the compose file pinned
    `${LINK_RETRIEVAL_MAX_CANDIDATES:-25}`, so the new code default was dead on arrival and
    the deploy looked entirely successful. Nothing failed; the retune simply did not happen.

    **What this does not guarantee, and it is the more important half.** On Coolify a
    `${NAME:-default}` reference is a *seed*, not a runtime default: Coolify parses it the
    first time it sees the variable, stores a UI entry holding that value, and from then on
    the UI entry is what reaches the container. Editing the fallback afterwards changes
    nothing in production. So this test guards the value a **fresh** environment is seeded
    with — a genuinely new variable, a rebuilt app, or a bare
    `docker compose -f docker-compose.prod.yml up` — and it cannot see Coolify at all.

    NEU-1088 is the worked example: K moved 25 → 35 in the code *and* here, CI was green, the
    deploy succeeded, and production went on running 25 because a Coolify entry seeded months
    earlier was shadowing the file. **Retuning a constant is a code change and a Coolify env
    change.** See the deploy checklist in `.claude/CLAUDE.md`.

    **CI is the enforcement point, and deliberately the only one.** There pytest runs against
    the checkout and the file is simply present. Inside the dev container it is not: it was
    briefly bind-mounted, and a single-file bind mount tracks the *inode*, so every
    `git checkout` between branches replaced the file and left the mount dangling — the whole
    suite then failed on a missing path that was sitting right there on the host. A static
    consistency check between two files in the repo is not worth breaking the local loop on
    every branch switch, so it skips here and runs there."""
    _set_required(monkeypatch)
    _clear_retrieval(monkeypatch)
    settings = Settings()  # type: ignore[call-arg]
    expected = getattr(settings, field)
    # Compared as the *field's* type, not as text: "0.10" and "0.1" are the same threshold,
    # and a test that insisted on the spelling would fail on a harmless reformat.
    assert type(expected)(_compose_fallback(env_name)) == expected
