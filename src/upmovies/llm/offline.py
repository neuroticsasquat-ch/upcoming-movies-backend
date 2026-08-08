"""The provider assumption the offline harnesses make, made explicit (NEU-983).

The `validate_*` and `diagnose_*` scripts each open an `AnthropicClient` directly and pass it a
model read from settings — `settings.cluster_model`, `settings.link_model` and so on. That was
unambiguous while every stage was Anthropic. It stopped being so when production moved to
DeepInfra (ADR-0012), and it leaves two hazards that want two different answers.

**A stage configured elsewhere.** `settings.cluster_model` is a DeepInfra model id in any
environment carrying the production values. Sending it to Anthropic fails — but it fails as an
API error about an unrecognized model, which reads as a broken script rather than as a
configuration the script cannot serve. `require_anthropic_stage` turns that into a refusal that
names the way out.

**A stage that really is Anthropic.** The subtler one, and why this module is not only a guard.
Run a validator in a dev container — where the compose file pins the four providers to
`anthropic` and sets no model vars — and it scores `claude-sonnet-4-6` quite happily while
production clusters on V4-Pro. Nothing is broken; the number is correct, and correct about a
model nobody is running. `stage_label` exists so that number arrives attached to what produced
it, and so a saved artifact cannot be misfiled later.

**Why this lives in `upmovies.llm` rather than in `scripts/`.** Not because the service uses it —
it must not, and a test pins that. Because `python scripts/foo.py` puts `scripts/` on `sys.path`
rather than the repo root, so a helper module *inside* `scripts/` is not importable by the very
scripts that need it, while `upmovies` is installed and imports from anywhere.

Neither half is the full provider axis. These scripts should eventually take `--provider` and
resolve through `Gateway`; until one of them has a question worth answering, this is the part
that stops a wrong answer being mistaken for a right one."""

import sys

from upmovies.config import Settings
from upmovies.llm.gateway import stage_models, stage_providers
from upmovies.llm.registry import ANTHROPIC

# Which env var moves each stage. Spelled out rather than computed for the same reason
# `stage_providers` is: `summarize` reads `SUMMARY_PROVIDER`, so there is no name to derive.
_PROVIDER_ENV = {
    "link": "LINK_PROVIDER",
    "cluster": "CLUSTER_PROVIDER",
    "source_judge": "SOURCE_JUDGE_PROVIDER",
    "summarize": "SUMMARY_PROVIDER",
}


def stage_label(settings: Settings, stage: str) -> str:
    """`provider=… model=…` for `stage`, for a harness's header line.

    Both axes, always. A bare `model=` cannot say which host answered, and two providers serving
    the same open weights at different prices are not the same measurement — the premise
    `pricing._RATES` is keyed on."""
    return f"provider={stage_providers(settings)[stage]} model={stage_models(settings)[stage]}"


def require_anthropic_stage(settings: Settings, stage: str) -> None:
    """Exit unless `stage` is configured for Anthropic; announce the pair when it is.

    `SystemExit` rather than a raised error because every caller is a CLI, where a traceback
    buries the one line the reader needs. `KeyError` for an unknown stage, which is a caller bug
    rather than a configuration anyone can fix."""
    provider = stage_providers(settings)[stage]
    model = stage_models(settings)[stage]
    if provider == ANTHROPIC:
        # Announced on **stderr** so a stdout being redirected into a file or a diff stays
        # exactly as it was. Unconditional: the run worth labelling is the one that succeeds,
        # because that is the one whose number gets saved.
        print(f"[{stage}] {stage_label(settings, stage)}", file=sys.stderr)
        return
    print(
        f"refusing to run: stage {stage!r} is configured for provider {provider!r} "
        f"(model {model!r}), and this script speaks only to Anthropic.\n"
        f"Sending that model id to Anthropic would fail as an unrecognized model.\n"
        f"Set {_PROVIDER_ENV[stage]}=anthropic (and the matching *_MODEL) for this run, or add "
        f"the --provider axis (NEU-983).",
        file=sys.stderr,
    )
    raise SystemExit(2)
