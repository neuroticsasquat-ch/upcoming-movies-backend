"""Per-stage provider resolution through the real `link` pipeline (NEU-980, design §5.3).

This is the structural claim the gateway exists to make good on. `run_link_ingest` runs three
of the four stages — `link`, `source_judge` and `cluster` — and used to serve all three from
one `AnthropicClient` threaded in from `run_link_stage`. One client instance cannot carry
three providers, so the test that matters is not "a gateway can resolve a provider" but "these
three stages resolve *separately*, in one run, and every row each one writes says which".
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from tests.fixtures.gateway import StubGateway
from upmovies.catalog.models import Film
from upmovies.ingest.models import LLMCall, RunLLMUsage
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm import CallResult, Usage
from upmovies.news.models import Story

_USAGE = Usage(input_tokens=1_000_000)

# One (provider, model) pair per stage, each priced differently in `pricing._RATES` — which is
# what lets the cost assertions below tell the three apart by their dollars alone.
_LINK = ("anthropic", "claude-haiku-4-5")
_JUDGE = ("deepseek", "deepseek-v4-flash")
_CLUSTER = ("deepinfra", "deepseek-ai/DeepSeek-V4-Flash")


class _StageClient:
    """Answers one stage and refuses the others, so a misrouted call fails loudly rather than
    being quietly served by whichever completer happened to be in hand."""

    def __init__(self, stage: str):
        self.stage = stage
        self.calls = 0

    async def complete_call(self, *, model, prompt, calls):
        marker = {
            "link": "entity-linking classifier",
            "source_judge": "source-quality rater",
            "cluster": "distinct EVENTS",
        }[self.stage]
        assert marker in prompt.stable_prefix, (
            f"the {self.stage} completer was handed a {marker!r}-less prompt — "
            f"a stage resolved the wrong provider"
        )
        self.calls += 1
        return calls.record(CallResult(text=self._reply(prompt), usage=_USAGE))

    def _reply(self, prompt) -> str:
        if self.stage == "link":
            stories = json.loads(prompt.user)["stories"]
            return json.dumps(
                [
                    {"id": s["id"], "film": 1, "confidence": 0.95, "reason": "about"}
                    for s in stories
                ]
            )
        if self.stage == "source_judge":
            items = json.loads(prompt.user)
            return json.dumps(
                [{"domain": i["domain"], "tier": "acceptable", "reason": "ok"} for i in items]
            )
        new_ns = [s["n"] for s in json.loads(prompt.user)["new_stories"]]
        return json.dumps(
            {
                "events": [
                    {
                        "existing": None,
                        "type": "trailer",
                        "confidence": "confirmed",
                        "stories": new_ns,
                    }
                ]
            }
        )


async def _rows(session, model, run_id):
    return list(
        (
            await session.execute(
                select(model).where(model.run_id == run_id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


async def test_link_source_judge_and_cluster_each_resolve_their_own_provider(
    session_factory, session, monkeypatch
):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add(
        Story(
            source="X",
            # A real public suffix, not `.example`: `normalize_domain` returns None for a
            # domain that has none, and the source-judge stage would then have nothing to judge.
            url="https://mshale.com/a",
            title="Runner wraps filming",
            published_at=datetime.now(UTC) - timedelta(days=1),
            link_status="pending",
            raw={"summary": ""},
        )
    )
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    async def _no_resolve(_client, _url):
        return None

    monkeypatch.setattr("upmovies.link.source_stage.resolve_google_news_url", _no_resolve)
    clients = {stage: _StageClient(stage) for stage in ("link", "source_judge", "cluster")}
    gateway = StubGateway(
        per_stage=clients,
        per_stage_provider={
            "link": _LINK[0],
            "source_judge": _JUDGE[0],
            "cluster": _CLUSTER[0],
        },
    )

    await run_link_ingest(
        session_factory=session_factory,
        gateway=gateway,
        run_id=run_id,
        model=_LINK[1],
        source_judge_model=_JUDGE[1],
        cluster_model=_CLUSTER[1],
        recency_days=45,
        batch_size=10,
        floor=0.7,
        source_gate_enabled=True,
    )

    # Every stage called out, and each one reached its own completer — the assertion inside
    # `_StageClient` is what makes "its own" mean something stronger than "a completer".
    assert {stage: c.calls for stage, c in clients.items()} == {
        "link": 1,
        "source_judge": 1,
        "cluster": 1,
    }
    assert sorted(set(gateway.resolved)) == ["cluster", "link", "source_judge"]

    calls = {r.stage: r for r in await _rows(session, LLMCall, run_id)}
    assert {s: (r.provider, r.model) for s, r in calls.items()} == {
        "link": _LINK,
        "source_judge": _JUDGE,
        "cluster": _CLUSTER,
    }

    # Same token count at all three stages, three different bills: the per-stage rows are
    # priced by the provider that answered, not by whichever one the run started with.
    usage = {r.stage: r for r in await _rows(session, RunLLMUsage, run_id)}
    assert usage["link"].cost_usd == Decimal("1.000000")  # anthropic haiku, $1.00/Mtok
    assert usage["source_judge"].cost_usd == Decimal("0.140000")  # deepseek flash, $0.14
    assert usage["cluster"].cost_usd == Decimal("0.090000")  # deepinfra flash, $0.09


async def test_a_stage_resolves_once_for_the_whole_stage(session_factory, session):
    """Not once per batch: the provider is a property of the stage, and a resolution per item
    would let one stage's rows straddle two providers if configuration changed mid-run."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    for n in range(3):
        session.add(
            Story(
                source="X",
                url=f"https://e/{n}",
                title="Runner wraps filming",
                published_at=datetime.now(UTC) - timedelta(days=1),
                link_status="pending",
                raw={"summary": ""},
            )
        )
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    link_client = _StageClient("link")
    gateway = StubGateway(per_stage={"link": link_client, "cluster": _StageClient("cluster")})

    await run_link_ingest(
        session_factory=session_factory,
        gateway=gateway,
        run_id=run_id,
        model=_LINK[1],
        cluster_model=_LINK[1],
        recency_days=45,
        batch_size=1,  # three batches, three calls
        floor=0.7,
    )

    assert link_client.calls == 3
    assert gateway.resolved.count("link") == 1
