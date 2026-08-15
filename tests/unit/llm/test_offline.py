"""`upmovies.llm.offline` — the provider assumption the offline harnesses make (NEU-983).

Every `validate_*` / `diagnose_*` script opens an `AnthropicClient` directly and passes it a
model read from settings. These pin the two halves of making that assumption visible: refuse
when it is false, label the run when it is true."""

import pytest

from upmovies.config import Settings
from upmovies.llm.offline import require_anthropic_stage, stage_label

_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
    "ADMIN_TOKEN": "t",
    "TMDB_API_KEY": "t",
    "ANTHROPIC_API_KEY": "t",
}


def _settings(**overrides) -> Settings:
    return Settings(**{**_BASE, **overrides})  # type: ignore[arg-type]


def test_a_stage_configured_for_anthropic_is_allowed():
    require_anthropic_stage(_settings(), "cluster")


def test_an_allowed_run_announces_what_it_verified_on_stderr(capsys):
    """The run worth labelling is the one that succeeds — that is the one whose number gets
    saved and read back later. On stderr so a redirected stdout stays parseable."""
    require_anthropic_stage(_settings(), "cluster")
    captured = capsys.readouterr()
    assert "provider=anthropic model=claude-sonnet-4-6" in captured.err
    assert captured.out == ""


def test_a_stage_configured_elsewhere_exits_rather_than_calling_anthropic():
    """What this prevents is not an exception — it is an Anthropic API error about an
    unrecognized model id, which reads as a broken script rather than as a configuration the
    script cannot serve."""
    settings = _settings(
        CLUSTER_PROVIDER="deepinfra",
        CLUSTER_MODEL="deepseek-ai/DeepSeek-V4-Pro",
        DEEPINFRA_API_KEY="k",
    )
    with pytest.raises(SystemExit) as exc:
        require_anthropic_stage(settings, "cluster")
    assert exc.value.code != 0


def test_the_refusal_names_the_stage_the_provider_the_model_and_the_way_out(capsys):
    settings = _settings(
        CLUSTER_PROVIDER="deepinfra",
        CLUSTER_MODEL="deepseek-ai/DeepSeek-V4-Pro",
        DEEPINFRA_API_KEY="k",
    )
    with pytest.raises(SystemExit):
        require_anthropic_stage(settings, "cluster")
    err = capsys.readouterr().err
    assert "cluster" in err
    assert "deepinfra" in err
    assert "deepseek-ai/DeepSeek-V4-Pro" in err
    # A reader who has just been refused should not have to go and find which env var to set.
    assert "CLUSTER_PROVIDER" in err


def test_the_summarize_stage_reads_the_summary_prefixed_settings():
    """`summarize` is the stage whose settings are spelled `SUMMARY_*`. Resolving it by
    `f"{stage}_provider"` would silently miss, so this must go through `stage_providers`."""
    settings = _settings(SUMMARY_PROVIDER="deepinfra", DEEPINFRA_API_KEY="k")
    with pytest.raises(SystemExit):
        require_anthropic_stage(settings, "summarize")


def test_stage_label_names_both_axes():
    """What makes a saved eval artifact self-describing. A bare `model=` cannot say which host
    answered, and two providers serving the same weights are not the same measurement."""
    assert stage_label(_settings(), "cluster") == "provider=anthropic model=claude-sonnet-4-6"


def test_stage_label_reflects_an_overridden_stage():
    settings = _settings(
        LINK_PROVIDER="deepinfra",
        LINK_MODEL="deepseek-ai/DeepSeek-V4-Flash",
        DEEPINFRA_API_KEY="k",
    )
    assert stage_label(settings, "link") == "provider=deepinfra model=deepseek-ai/DeepSeek-V4-Flash"


def test_an_unknown_stage_is_a_programming_error():
    with pytest.raises(KeyError):
        require_anthropic_stage(_settings(), "nonesuch")


def test_offline_helpers_are_not_reachable_from_the_service():
    """A guard about eval tooling has no business in a request path. Pinned rather than trusted:
    the module is easy to import from anywhere once it exists."""
    import subprocess

    hits = subprocess.run(
        ["grep", "-rn", "llm.offline", "src/upmovies/routers", "src/upmovies/main.py"],
        capture_output=True,
        text=True,
    )
    assert hits.stdout == ""
