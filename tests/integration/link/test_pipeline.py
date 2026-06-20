import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.news.models import Event, Story


class FakeClient:
    """Routes on the system prompt: link decisions for Stage 1, one new event for Stage 2."""

    async def complete(self, *, model, system, messages, max_tokens=4096) -> str:
        if "entity-linking classifier" in system[0]["text"]:
            stories = json.loads(messages[0]["content"])
            return json.dumps(
                [{"id": s["id"], "film": 1, "confidence": 0.95, "reason": "about"} for s in stories]
            )
        new_ids = [s["id"] for s in json.loads(messages[0]["content"])["new_stories"]]
        return json.dumps(
            {
                "events": [
                    {
                        "existing": None,
                        "type": "trailer",
                        "confidence": "confirmed",
                        "stories": new_ids,
                    }
                ]
            }
        )


async def _story(url, *, published_offset_days, status="pending"):
    now = datetime.now(UTC)
    return Story(
        source="X",
        url=url,
        title="Runner news",
        published_at=now - timedelta(days=published_offset_days),
        link_status=status,
        raw={"summary": ""},
    )


async def _run(session, run_id):
    return await run_link_ingest(
        session_factory=lambda: session,
        client=FakeClient(),
        run_id=run_id,
        model="link-m",
        cluster_model="cluster-m",
        recency_days=45,
        batch_size=10,
        floor=0.7,
    )


async def test_links_then_clusters_recent_pending(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add_all(
        [
            await _story("https://e/recent", published_offset_days=2),
            await _story("https://e/old", published_offset_days=400),
        ]
    )
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await _run(session, run_id)

    assert result.linked == 1
    rows = {
        s.url: s
        for s in (
            await session.execute(select(Story), execution_options={"populate_existing": True})
        )
        .scalars()
        .all()
    }
    assert rows["https://e/recent"].link_status == "linked"
    assert rows["https://e/recent"].film_id == film.id
    assert rows["https://e/old"].link_status == "pending"

    events = (await session.execute(select(Event).where(Event.film_id == film.id))).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == "trailer"

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert run.detail and "linked 1" in run.detail and "1 events" in run.detail


async def test_rerun_is_noop_when_fully_processed(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await _run(session, run_id)
    assert result.linked == 0 and result.rejected == 0
