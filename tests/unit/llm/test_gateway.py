"""`Gateway` — per-stage provider resolution (NEU-980, design §5.3, §8).

The seam these tests defend: one gateway lifecycle backs four stages that may each be on a
different provider, and resolving the wrong one is not a crash — it is an eval run that
attributes a provider's latency, cost and coverage to a different provider entirely.
"""

import pytest
from pydantic import ValidationError

from upmovies.config import Settings
from upmovies.llm.anthropic import AnthropicClient
from upmovies.llm.gateway import (
    STAGES,
    Gateway,
    MissingCredentialError,
    credential_for,
)
from upmovies.llm.openai_compat import OpenAICompatClient
from upmovies.llm.retry import RetryPolicy

_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://a:b@c:5432/d",
    "ADMIN_TOKEN": "xxx",
    "TMDB_API_KEY": "tmdb-xxx",
    "ANTHROPIC_API_KEY": "anthropic-xxx",
}

# Cleared unless a test sets them: the container passes empty strings for the two optional
# credentials, and the dev compose file may pass provider names too.
_GATEWAY_ENV = (
    "DEEPINFRA_API_KEY",
    "DEEPSEEK_API_KEY",
    "LINK_PROVIDER",
    "CLUSTER_PROVIDER",
    "SOURCE_JUDGE_PROVIDER",
    "SUMMARY_PROVIDER",
)


def _settings(monkeypatch, **env: str) -> Settings:
    for key, value in {**_REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    for key in _GATEWAY_ENV:
        if key not in env:
            monkeypatch.delenv(key, raising=False)
    return Settings()  # type: ignore[call-arg]


# --- the default: four stages, one provider, today's behaviour ------------------


async def test_every_stage_defaults_to_anthropic(monkeypatch):
    """The exit criterion is capability, not migration (spec §1): with nothing configured,
    all four stages must still be served by the same adapter they were before the gateway."""
    async with Gateway(_settings(monkeypatch)) as gw:
        for stage in STAGES:
            assert gw.provider_for(stage) == "anthropic"
            assert isinstance(gw.for_stage(stage), AnthropicClient)


async def test_stages_on_one_provider_share_a_single_client(monkeypatch):
    """One lifecycle, N pooled clients — N being providers, not stages. Three link-pipeline
    stages on Anthropic must not open three connection pools."""
    async with Gateway(_settings(monkeypatch)) as gw:
        assert gw.for_stage("link") is gw.for_stage("cluster") is gw.for_stage("source_judge")


async def test_repeated_lookups_return_the_same_client(monkeypatch):
    async with Gateway(_settings(monkeypatch)) as gw:
        assert gw.for_stage("link") is gw.for_stage("link")


# --- per-stage configuration ----------------------------------------------------


async def test_each_stage_resolves_its_own_configured_provider(monkeypatch):
    settings = _settings(
        monkeypatch,
        CLUSTER_PROVIDER="deepinfra",
        SUMMARY_PROVIDER="deepseek",
        DEEPINFRA_API_KEY="di-xxx",
        DEEPSEEK_API_KEY="ds-xxx",
    )
    async with Gateway(settings) as gw:
        assert gw.provider_for("link") == "anthropic"
        assert gw.provider_for("cluster") == "deepinfra"
        assert gw.provider_for("source_judge") == "anthropic"
        assert gw.provider_for("summarize") == "deepseek"
        assert isinstance(gw.for_stage("link"), AnthropicClient)
        assert isinstance(gw.for_stage("cluster"), OpenAICompatClient)
        assert isinstance(gw.for_stage("summarize"), OpenAICompatClient)
        # Two OpenAI-compatible stages, two different hosts — never one pooled client.
        assert gw.for_stage("cluster") is not gw.for_stage("summarize")


async def test_summary_provider_setting_drives_the_summarize_stage(monkeypatch):
    """The setting is `SUMMARY_PROVIDER` while the stage is `summarize` — the one place the
    two vocabularies differ, and a mapping worth pinning rather than inferring."""
    settings = _settings(monkeypatch, SUMMARY_PROVIDER="deepinfra", DEEPINFRA_API_KEY="di-xxx")
    async with Gateway(settings) as gw:
        assert gw.provider_for("summarize") == "deepinfra"


async def test_an_unknown_provider_name_fails_at_boot(monkeypatch):
    """The `Literal` buys provider-name validation for free (spec §8): a typo is a container
    that will not start, not a stage that dies at its first call."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, CLUSTER_PROVIDER="deepinfrra")


# --- eval overrides -------------------------------------------------------------


async def test_overrides_win_over_configured_providers(monkeypatch):
    """What an eval run varies. Model stays a pipeline argument (spec §5.3) — this is the
    other axis, and it must not require an env change to sweep."""
    settings = _settings(monkeypatch, DEEPSEEK_API_KEY="ds-xxx")
    async with Gateway(settings, overrides={"cluster": "deepseek"}) as gw:
        assert gw.provider_for("cluster") == "deepseek"
        assert gw.provider_for("link") == "anthropic"


async def test_an_override_for_an_unknown_stage_is_rejected(monkeypatch):
    """A silently ignored override is an eval run that measures the wrong thing and says so
    convincingly, so it is refused at construction rather than dropped."""
    with pytest.raises(ValueError, match="summarise"):
        Gateway(_settings(monkeypatch), overrides={"summarise": "deepseek"})


async def test_an_override_naming_an_unknown_provider_is_rejected(monkeypatch):
    """Overrides bypass the `Literal`, so they are validated here instead."""
    with pytest.raises(ValueError, match="mistral"):
        Gateway(_settings(monkeypatch), overrides={"cluster": "mistral"})


# --- credentials, and the absence of fallback -----------------------------------


async def test_a_stage_with_no_credential_raises_rather_than_falling_back(monkeypatch):
    """No cross-provider fallback, ever (spec §9). Silently answering from Anthropic would
    attribute one provider's results to another — the one failure mode an eval run cannot
    detect afterwards."""
    settings = _settings(monkeypatch, CLUSTER_PROVIDER="deepinfra")
    async with Gateway(settings) as gw:
        with pytest.raises(MissingCredentialError, match="DEEPINFRA_API_KEY"):
            gw.for_stage("cluster")


async def test_an_empty_credential_counts_as_missing(monkeypatch):
    """`DEEPINFRA_API_KEY: "${DEEPINFRA_API_KEY:-}"` in the dev compose file means the
    container's normal state is an empty string, not an unset variable."""
    settings = _settings(monkeypatch, CLUSTER_PROVIDER="deepinfra", DEEPINFRA_API_KEY="")
    async with Gateway(settings) as gw:
        with pytest.raises(MissingCredentialError):
            gw.for_stage("cluster")


async def test_a_missing_credential_does_not_disturb_the_other_stages(monkeypatch):
    """Clients are built per stage on first use, so one unusable stage is one failing stage —
    the same per-item isolation the pipelines already rely on."""
    settings = _settings(monkeypatch, CLUSTER_PROVIDER="deepseek")
    async with Gateway(settings) as gw:
        assert isinstance(gw.for_stage("link"), AnthropicClient)


def test_credential_for_reports_absence_without_raising(monkeypatch):
    """The resolver the offline scripts share: they skip a provider they have no key for,
    while the gateway refuses to run a stage without one."""
    settings = _settings(monkeypatch, DEEPSEEK_API_KEY="ds-xxx")
    assert credential_for(settings, "anthropic") == "anthropic-xxx"
    assert credential_for(settings, "deepseek") == "ds-xxx"
    assert credential_for(settings, "deepinfra") is None


def test_credential_for_rejects_an_unknown_provider(monkeypatch):
    with pytest.raises(KeyError):
        credential_for(_settings(monkeypatch), "mistral")


# --- lifecycle ------------------------------------------------------------------


async def test_an_unknown_stage_is_refused(monkeypatch):
    async with Gateway(_settings(monkeypatch)) as gw:
        with pytest.raises(ValueError, match="translate"):
            gw.for_stage("translate")


async def test_exit_closes_every_client_it_opened(monkeypatch):
    """One lifecycle: the stages borrow clients and the gateway owns them, so nothing leaks a
    connection pool when a pipeline returns."""
    settings = _settings(monkeypatch, CLUSTER_PROVIDER="deepinfra", DEEPINFRA_API_KEY="di-xxx")
    async with Gateway(settings) as gw:
        anthropic = gw.for_stage("link")
        deepinfra = gw.for_stage("cluster")
    assert isinstance(anthropic, AnthropicClient)
    assert isinstance(deepinfra, OpenAICompatClient)
    # Reaching for the underlying httpx client is the only way to observe the pool from
    # outside; both adapters wrap one.
    assert deepinfra._client.is_closed
    assert anthropic._client.is_closed()


async def test_exit_closes_clients_even_when_the_body_raised(monkeypatch):
    gw = Gateway(_settings(monkeypatch))
    opened: list[AnthropicClient] = []
    with pytest.raises(RuntimeError):
        async with gw:
            client = gw.for_stage("link")
            assert isinstance(client, AnthropicClient)
            opened.append(client)
            raise RuntimeError("stage crashed")
    assert opened[0]._client.is_closed()


async def test_a_closed_gateway_refuses_to_hand_out_another_client(monkeypatch):
    """Otherwise a lookup after exit would build a client onto an unwound stack: live, never
    closed, and indistinguishable from one the gateway is still managing."""
    gw = Gateway(_settings(monkeypatch))
    async with gw:
        gw.for_stage("link")
    with pytest.raises(RuntimeError, match="closed"):
        gw.for_stage("link")


async def test_the_shared_retry_policy_reaches_both_adapters(monkeypatch):
    """Retries are shared, not merely matched (spec §9): a gateway configured with one policy
    must not hand one adapter the default and the other the policy it was given."""
    policy = RetryPolicy(max_retries=7, timeout=11.0)
    settings = _settings(monkeypatch, CLUSTER_PROVIDER="deepinfra", DEEPINFRA_API_KEY="di-xxx")
    async with Gateway(settings, policy=policy) as gw:
        link = gw.for_stage("link")
        cluster = gw.for_stage("cluster")
    assert isinstance(link, AnthropicClient)
    assert isinstance(cluster, OpenAICompatClient)
    assert link._policy is policy
    assert cluster._policy is policy


def test_stages_are_the_four_the_schema_allows():
    """The set is closed and enforced in the schema (`ck_run_llm_usage_stage`, CONTEXT.md);
    a fifth stage here would resolve a provider for a stage no telemetry row can name."""
    assert STAGES == ("link", "cluster", "source_judge", "summarize")
