from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm.client import Usage
from upmovies.news.models import Event, SourceDomain, Story


class _StubClient:
    """LinkClient stub: linking is skipped (no pending stories), cluster returns one event."""

    async def complete_with_usage(
        self, *, model: str, system: list, messages: list, max_tokens: int = 4096
    ) -> tuple[str, Usage]:
        # Domain judge for unknown domains -> mshale.com low.
        if "source-quality rater" in system[0]["text"]:
            return '[{"domain": "mshale.com", "tier": "low", "reason": "farm"}]', Usage()
        # Cluster call -> one confirmed casting event over story n=1.
        cluster_resp = (
            '{"events": [{"existing": null, "type": "casting",'
            ' "confidence": "confirmed", "cast": ["Test Performer"], "stories": [1]}]}'
        )
        return cluster_resp, Usage()

    async def complete_batch(
        self, requests: list, *, poll_interval: float = 15.0, timeout: float = 3600.0
    ) -> dict:
        return {}


async def test_pipeline_gate_downgrades_low_trust(session_factory, session):
    film = Film(tmdb_id=555, slug="pipe-gate", title="Gate Film")
    session.add(film)
    await session.flush()
    story = Story(
        source="x",
        url="https://mshale.com/a",
        title="Big casting",
        film_id=film.id,
        link_status="linked",
        fetched_at=datetime.now(UTC),
    )
    session.add(story)
    await session.commit()

    async def _resolver(c, url):
        return None

    # Patch the resolver used by the stage via the source_gate_enabled path.
    from upmovies.link import source_stage

    orig = source_stage.resolve_google_news_url
    source_stage.resolve_google_news_url = _resolver
    try:
        # run_id: create a run row first (mirror other pipeline tests' helper if present).
        from upmovies.ingest.runs import create_run

        run_id = await create_run(session, kind="link")
        await session.commit()
        await run_link_ingest(
            session_factory=session_factory,
            client=_StubClient(),
            run_id=run_id,
            model="claude-haiku-4-5",
            cluster_model="claude-haiku-4-5",
            recency_days=30,
            batch_size=10,
            floor=0.7,
            source_gate_enabled=True,
            source_judge_model="claude-haiku-4-5",
            unresolved_tier="acceptable",
        )
    finally:
        source_stage.resolve_google_news_url = orig

    event = (await session.execute(select(Event).where(Event.film_id == film.id))).scalar_one()
    assert event.confidence == "rumored"
    judged = (
        await session.execute(select(SourceDomain).where(SourceDomain.domain == "mshale.com"))
    ).scalar_one()
    assert judged.llm_tier == "low"
