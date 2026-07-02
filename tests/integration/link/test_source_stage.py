from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.link.source_stage import run_source_quality_stage
from upmovies.llm.client import Usage
from upmovies.news.models import SourceDomain, Story


class _FakeClient:
    def __init__(self, text):
        self._text = text
        self.calls = 0

    async def complete_with_usage(self, *, model, system, messages, max_tokens: int = 1024):
        self.calls += 1
        return self._text, Usage(input_tokens=1, output_tokens=1)


async def _film(session, slug):
    film = Film(tmdb_id=abs(hash(slug)) % 10_000_000, slug=slug, title="F")
    session.add(film)
    await session.flush()
    return film


async def _linked_story(session, film_id, url):
    s = Story(
        source="x",
        url=url,
        title="t",
        film_id=film_id,
        link_status="linked",
        fetched_at=datetime.now(UTC),
    )
    session.add(s)
    await session.flush()
    return s


async def test_judges_unknown_domains_and_blocks(session_factory, session):
    film = await _film(session, "ss1")
    s_low = await _linked_story(session, film.id, "https://mshale.com/a")
    s_ok = await _linked_story(session, film.id, "https://variety.com/b")
    # Pre-block a domain via admin override so the story is hard-dropped.
    now = datetime.now(UTC)
    session.add(
        SourceDomain(
            domain="variety.com", admin_override="block", first_seen_at=now, updated_at=now
        )
    )
    await session.commit()

    client = _FakeClient('[{"domain": "mshale.com", "tier": "low", "reason": "farm"}]')

    async def _resolver(c, url):
        return None  # no Google URLs here

    result, usage = await run_source_quality_stage(
        session_factory=session_factory, client=client, judge_model="m", resolver=_resolver
    )

    assert result.judged == 1  # only mshale.com was unknown (variety.com pre-existed)
    assert result.blocked == 1  # variety.com story hard-dropped
    row = (
        await session.execute(
            select(Story).where(Story.id == s_ok.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.link_status == "rejected" and row.link_note == "source-blocked"
    kept = (
        await session.execute(
            select(Story).where(Story.id == s_low.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert kept.link_status == "linked"
    judged = (
        await session.execute(select(SourceDomain).where(SourceDomain.domain == "mshale.com"))
    ).scalar_one()
    assert judged.llm_tier == "low"


async def test_resolves_google_urls_before_judging(session_factory, session):
    film = await _film(session, "ss2")
    s = await _linked_story(session, film.id, "https://news.google.com/rss/articles/ABC")
    await session.commit()

    client = _FakeClient('[{"domain": "deadline.com", "tier": "trusted", "reason": "trade"}]')

    async def _resolver(c, url):
        return "https://deadline.com/real-story"

    result, usage = await run_source_quality_stage(
        session_factory=session_factory, client=client, judge_model="m", resolver=_resolver
    )
    assert result.resolved == 1
    row = (
        await session.execute(
            select(Story).where(Story.id == s.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.resolved_url == "https://deadline.com/real-story"
    assert row.resolve_state == "resolved"
    judged = (
        await session.execute(select(SourceDomain).where(SourceDomain.domain == "deadline.com"))
    ).scalar_one()
    assert judged.llm_tier == "trusted"


async def test_empty_when_nothing_linked(session_factory):
    client = _FakeClient("[]")

    async def _resolver(c, url):
        return None

    result, usage = await run_source_quality_stage(
        session_factory=session_factory, client=client, judge_model="m", resolver=_resolver
    )
    assert result == type(result)(resolved=0, judged=0, blocked=0)
    assert client.calls == 0


async def test_backfill_judges_all_resolved_domains(session_factory, session):
    from uuid import uuid4

    from upmovies.link.source_stage import backfill_source_domains
    from upmovies.news.models import Story
    from upmovies.news.source_quality import list_source_domains

    # Two resolved stories + one direct trade-feed story (resolve_state='none')
    # + one unresolved (no domain -> skipped).
    session.add_all(
        [
            Story(
                id=uuid4(),
                source="X",
                url="https://news.google.com/rss/1",
                resolved_url="https://www.variety.com/a",
                resolve_state="resolved",
                title="Variety story",
            ),
            Story(
                id=uuid4(),
                source="X",
                url="https://www.mshale.com/b",
                resolved_url="https://www.mshale.com/b",
                resolve_state="resolved",
                title="Mshale story",
            ),
            Story(
                id=uuid4(),
                source="X",
                url="https://deadline.com/article/123",
                resolved_url="https://deadline.com/article/123",
                resolve_state="none",
                title="Direct trade-feed story",
            ),
            Story(
                id=uuid4(),
                source="X",
                url="https://news.google.com/rss/2",
                resolved_url=None,
                resolve_state="pending",
                title="unresolved",
            ),
        ]
    )
    await session.commit()

    class _Client:
        async def complete_with_usage(self, *, model, system, messages, max_tokens=4096):
            import json

            from upmovies.llm.client import Usage

            items = json.loads(messages[0]["content"])
            arr = [{"domain": it["domain"], "tier": "acceptable", "reason": "ok"} for it in items]
            return json.dumps(arr), Usage(output_tokens=3)

    judged = await backfill_source_domains(
        session_factory=session_factory, client=_Client(), judge_model="claude-haiku-4-5"
    )
    rows = await list_source_domains(session)
    domains = {r.domain for r in rows}
    # After removing the resolve_state filter, the backfill now includes direct trade-feed
    # stories (resolve_state='none'), so deadline.com should be judged.
    assert "deadline.com" in domains, f"deadline.com not in {domains}"
    assert "variety.com" in domains, f"variety.com not in {domains}"
    assert "mshale.com" in domains, f"mshale.com not in {domains}"
    # Verify the count matches what we actually got
    assert judged == len(domains), f"judged={judged} but {len(domains)} rows created: {domains}"

    # Idempotent: a second run judges nothing new.
    again = await backfill_source_domains(
        session_factory=session_factory, client=_Client(), judge_model="claude-haiku-4-5"
    )
    assert again == 0
