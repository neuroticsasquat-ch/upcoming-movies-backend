"""A `StageGateway` for tests that care about the pipeline, not about provider resolution.

Most pipeline tests want one fake completer answering every stage; the gateway is then just
the plumbing that gets it there, and `StubGateway(client)` says so in one line. The tests that
care which provider answers what pass `per_stage` and read `resolved` afterwards.

`Gateway` itself is unit-tested against real adapters in `tests/unit/llm/test_gateway.py` —
this stub deliberately does not stand in for that."""

from collections.abc import Mapping

from upmovies.llm.types import Completer


class StubGateway:
    """Resolves every stage to `client`, or to its own entry in `per_stage` when given one.

    `provider` names what the telemetry rows should record; `per_stage_provider` varies it per
    stage for the tests that check a row is attributed to the provider that actually answered.
    Every lookup is appended to `resolved`, so a test can assert a stage resolved once rather
    than per item."""

    def __init__(
        self,
        client: Completer | None = None,
        *,
        per_stage: Mapping[str, Completer] | None = None,
        provider: str = "anthropic",
        per_stage_provider: Mapping[str, str] | None = None,
    ):
        self._client = client
        self._per_stage = dict(per_stage or {})
        self._provider = provider
        self._per_stage_provider = dict(per_stage_provider or {})
        self.resolved: list[str] = []

    def for_stage(self, stage: str) -> Completer:
        self.resolved.append(stage)
        client = self._per_stage.get(stage, self._client)
        if client is None:
            raise AssertionError(f"no stub completer configured for stage {stage!r}")
        return client

    def provider_for(self, stage: str) -> str:
        return self._per_stage_provider.get(stage, self._provider)
