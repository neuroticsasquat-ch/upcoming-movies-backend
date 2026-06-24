import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import RunLLMUsage
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm.client import BatchResult, Usage
from upmovies.news.models import Story


class UsageFakeClient:
    """Returns a fixed Usage on both surfaces, routing decisions on the same prompt sniff
    the production FakeClient uses (entity-linking classifier vs cluster)."""

    def __init__(self, usage: Usage):
        self._usage = usage

    async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
        return self._decide(system, messages), self._usage

    async def complete_batch(self, requests, *, poll_interval=15.0, timeout=3600.0) -> dict:
        return {
            r.custom_id: BatchResult(
                custom_id=r.custom_id,
                ok=True,
                text=self._decide(r.system, r.messages),
                usage=self._usage,
            )
            for r in requests
        }

    def _decide(self, system, messages) -> str:
        if "entity-linking classifier" in system[0]["text"]:
            stories = json.loads(messages[0]["content"])
            return json.dumps(
                [{"id": s["id"], "film": 1, "confidence": 0.95, "reason": "about"} for s in stories]
            )
        new_ns = [s["n"] for s in json.loads(messages[0]["content"])["new_stories"]]
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


async def _story(session, url):
    s = Story(
        source="X",
        url=url,
        title="Runner news",
        published_at=datetime.now(UTC) - timedelta(days=1),
        link_status="pending",
        raw={"summary": ""},
    )
    session.add(s)
    await session.flush()
    return s


async def test_run_link_ingest_records_link_and_cluster_usage(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await _story(session, "https://e/1")
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    usage = Usage(input_tokens=100, output_tokens=10, cache_read_input_tokens=900)
    await run_link_ingest(
        session_factory=lambda: session,
        client=UsageFakeClient(usage),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=45,
        batch_size=10,
        floor=0.7,
        use_batches=False,
    )

    rows = {
        r.stage: r
        for r in (
            await session.execute(
                select(RunLLMUsage).where(RunLLMUsage.run_id == run_id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    }
    assert set(rows) == {"link", "cluster"}
    assert rows["link"].model == "claude-haiku-4-5"
    assert rows["link"].batched is False
    assert rows["link"].input_tokens == 100
    assert rows["link"].output_tokens == 10
    assert rows["link"].cache_read_input_tokens == 900
    assert rows["cluster"].model == "claude-sonnet-4-6"
    assert rows["cluster"].input_tokens == 100


async def test_run_link_ingest_records_batched_flag(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await _story(session, "https://e/1")
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    await run_link_ingest(
        session_factory=lambda: session,
        client=UsageFakeClient(Usage(input_tokens=5)),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=45,
        batch_size=10,
        floor=0.7,
        use_batches=True,
    )

    rows = {
        r.stage: r
        for r in (await session.execute(select(RunLLMUsage).where(RunLLMUsage.run_id == run_id)))
        .scalars()
        .all()
    }
    assert rows["link"].batched is True
    assert rows["cluster"].batched is True
