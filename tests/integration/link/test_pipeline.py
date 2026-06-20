import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.news.models import Story


class FakeClient:
    """Returns 'about #1' for every story in the batch."""

    async def complete(self, *, model, system, messages, max_tokens=4096) -> str:
        stories = json.loads(messages[0]["content"])
        return json.dumps(
            [{"id": s["id"], "film": 1, "confidence": 0.95, "reason": "about"} for s in stories]
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


async def test_links_recent_pending_skips_old_and_finalizes(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    recent = await _story("https://e/recent", published_offset_days=2)
    old = await _story("https://e/old", published_offset_days=400)
    session.add_all([recent, old])
    await session.commit()

    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await run_link_ingest(
        session_factory=lambda: session,
        client=FakeClient(),
        run_id=run_id,
        model="m",
        recency_days=45,
        batch_size=10,
        floor=0.7,
    )

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
    assert rows["https://e/old"].link_status == "pending"  # outside the recency window

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.status == "succeeded"
    assert run.items_processed == 1
    assert run.detail and "linked 1" in run.detail


async def test_rerun_is_noop_when_nothing_pending(session):
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    session.add(await _story("https://e/done", published_offset_days=1, status="linked"))
    await session.commit()
    run_id = await create_run(session, kind="link")
    await session.commit()

    result = await run_link_ingest(
        session_factory=lambda: session,
        client=FakeClient(),
        run_id=run_id,
        model="m",
        recency_days=45,
        batch_size=10,
        floor=0.7,
    )
    assert result.linked == 0 and result.rejected == 0
