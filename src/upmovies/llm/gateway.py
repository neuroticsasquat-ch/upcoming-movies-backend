"""Which provider answers which stage, and one lifecycle over the clients that do it.

`run_link_stage` used to open a single `AnthropicClient` and pass it down, and that one
instance backed **three** call sites — link, cluster and source_judge all run inside
`run_link_ingest`. Per-stage providers cannot be threaded through one client instance, which
is the structural reason this module exists rather than a settings lookup at each call site
(design §5.3). The two pipelines now take a gateway, and each stage resolves its own completer
from it.

Clients are pooled by **provider, not by stage**: the default configuration is four stages on
Anthropic, and that must stay one connection pool rather than four. They are also built on
first use, so a stage nobody asks for costs nothing — a run with no unknown domains to judge
needs no `source_judge` credential.

What lazy building does *not* buy is isolation. A stage resolves before its own per-item loop,
outside the `try` that isolates a failed chunk, so a `MissingCredentialError` propagates out of
the pipeline and the stages after it never run. That is the intended shape: §9's "a failing
provider means a failing chunk" is about a provider that answers badly, and a stage with no
credential is a configuration error to fail the run on, not a chunk to skip. NEU-981 moves the
detection to boot, where it belongs.

`model` is deliberately *not* resolved here. It stays an explicit argument on both pipeline
signatures because `scripts/eval_cluster_diff.py`'s entire A/B mechanism is `--model X` then
`--model Y` against a fixed corpus; folding the model into the gateway would look like a deeper
module and force that harness to be rewritten instead of extended (design §5.3, §11).
"""

from collections.abc import Mapping
from contextlib import AsyncExitStack

from upmovies.config import Settings
from upmovies.llm.anthropic import AnthropicClient
from upmovies.llm.openai_compat import OpenAICompatClient
from upmovies.llm.registry import ANTHROPIC, DEEPINFRA, DEEPSEEK, PROVIDERS
from upmovies.llm.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from upmovies.llm.types import Completer

# The four stages a provider can be configured for. Closed, and enforced as such further down
# the line — `ingest.llm_call` and `run_llm_usage` both check-constrain `stage` to the same
# four, so a fifth here would resolve a provider for calls no telemetry row could name.
# (`ingest.runs.STAGE_KINDS` is a different, narrower list: it classifies the three stages the
# total-failure guard reads counters for, and `source_judge` keeps none.)
STAGES: tuple[str, ...] = ("link", "cluster", "source_judge", "summarize")


class MissingCredentialError(RuntimeError):
    """A stage is configured for a provider whose API key is unset.

    Raised rather than resolved to Anthropic. There is no cross-provider fallback anywhere in
    the gateway (design §9): production is Anthropic for all four stages regardless under the
    capability-only exit criterion, so falling back buys nothing — while during an eval run it
    would silently attribute one provider's latency, cost and coverage to another, which is the
    one failure mode the numbers afterwards cannot reveal."""


def credential_for(settings: Settings, provider: str) -> str | None:
    """The configured API key for `provider`, or None when it is unset **or empty**.

    Empty counts as unset because that is the container's normal state: the dev compose file
    passes `DEEPINFRA_API_KEY: "${DEEPINFRA_API_KEY:-}"`, so an unconfigured credential arrives
    as `""` rather than as an absent variable.

    Raises `KeyError` for a provider with no entry — the same discipline as `rates_for` and
    `base_url_for`, and the reason the offline scripts can share this resolver: they skip a
    provider they have no key for, while `Gateway` refuses to run a stage without one."""
    keys: dict[str, str | None] = {
        ANTHROPIC: settings.anthropic_api_key,
        DEEPINFRA: settings.deepinfra_api_key,
        DEEPSEEK: settings.deepseek_api_key,
    }
    return keys[provider] or None


class Gateway:
    """Resolves a `Completer` per stage, and owns the clients it builds doing so.

        async with Gateway(settings) as gw:
            completer = gw.for_stage("cluster")

    `overrides` maps a stage to a provider for one run, winning over the configured settings.
    That is the axis an eval sweep varies; the model axis is the pipelines' own `model=`
    argument, untouched by this."""

    def __init__(
        self,
        settings: Settings,
        *,
        overrides: Mapping[str, str] | None = None,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ):
        self._settings = settings
        self._policy = policy
        # Written out rather than resolved by `getattr(settings, f"{stage}_provider")`: the
        # summarize stage reads `SUMMARY_PROVIDER`, so there is no name to compute, and an
        # explicit map is what makes that mismatch visible instead of surprising.
        self._providers: dict[str, str] = {
            "link": settings.link_provider,
            "cluster": settings.cluster_provider,
            "source_judge": settings.source_judge_provider,
            "summarize": settings.summary_provider,
        }
        for stage, provider in (overrides or {}).items():
            # Both halves are validated here because both bypass the `Literal` that validates
            # the settings at boot. An override that is quietly ignored is worse than one that
            # is refused: the run measures the provider it was meant to replace, and reports
            # it under the name of the one that never ran.
            if stage not in self._providers:
                raise ValueError(
                    f"override for unknown stage {stage!r}: expected one of {', '.join(STAGES)}"
                )
            if provider not in PROVIDERS:
                raise ValueError(
                    f"override names unknown provider {provider!r}: "
                    f"expected one of {', '.join(PROVIDERS)}"
                )
            self._providers[stage] = provider
        self._clients: dict[str, Completer] = {}
        self._stack = AsyncExitStack()
        self._closed = False

    async def __aenter__(self) -> "Gateway":
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Flagged, not just unwound: without it a `for_stage` after exit would build a fresh
        # client onto an already-unwound stack — one nothing would ever close, handed back as
        # though the gateway were still open.
        self._closed = True
        await self._stack.aclose()

    def provider_for(self, stage: str) -> str:
        """Which provider answers `stage`.

        The telemetry rows need this as much as the call does: `rates_for` is keyed on
        `(provider, model)`, so a row that assumes the provider prices the call at another
        host's rates — and a cost comparison between two providers is then measuring the
        assumption."""
        try:
            return self._providers[stage]
        except KeyError:
            raise ValueError(
                f"unknown stage {stage!r}: expected one of {', '.join(STAGES)}"
            ) from None

    def for_stage(self, stage: str) -> Completer:
        """The completer bound to `stage`'s provider, built on first use and pooled by
        provider thereafter. Raises `MissingCredentialError` when that provider has no key."""
        if self._closed:
            raise RuntimeError("this Gateway is closed: its clients have already been released")
        provider = self.provider_for(stage)
        client = self._clients.get(provider)
        if client is None:
            client = self._clients[provider] = self._build(provider)
        return client

    def _build(self, provider: str) -> Completer:
        api_key = credential_for(self._settings, provider)
        if api_key is None:
            raise MissingCredentialError(
                f"a stage is configured for provider {provider!r} but "
                f"{provider.upper()}_API_KEY is unset"
            )
        client = (
            AnthropicClient(api_key=api_key, policy=self._policy)
            if provider == ANTHROPIC
            else OpenAICompatClient(provider=provider, api_key=api_key, policy=self._policy)
        )
        # Registered as a callback rather than entered as a context manager because clients
        # are built lazily, from a synchronous lookup. The stack still unwinds every one of
        # them on exit, including when an earlier close raises.
        self._stack.push_async_callback(client.aclose)
        return client
