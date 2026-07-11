from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.link.cluster import ClusterPlan, apply_cluster_decisions
from upmovies.news.models import Event, SourceDomain, Story


async def _film(session, slug):
    film = Film(tmdb_id=abs(hash(slug)) % 10_000_000, slug=slug, title="A Film")
    session.add(film)
    await session.flush()
    return film


async def _story(session, film_id, url):
    story = Story(
        source="Google News",
        url=url,
        title="Big beat",
        film_id=film_id,
        link_status="linked",
        fetched_at=datetime.now(UTC),
    )
    session.add(story)
    await session.flush()
    return story


async def _run(session, film_id, story_ids, *, confidence):
    plan = ClusterPlan(film_id=film_id, existing_event_ids=[], unclustered_story_ids=story_ids)
    stories_list = ", ".join(str(i + 1) for i in range(len(story_ids)))
    raw = (
        f'{{"events": [{{"existing": null, "type": "casting", "confidence": "{confidence}",'
        f' "cast": ["Test Performer"], "stories": [{stories_list}]}}]}}'
    )
    return await apply_cluster_decisions(session, plan=plan, raw=raw, unresolved_tier="acceptable")


async def test_all_low_trust_downgrades_to_rumored(session):
    film = await _film(session, "g-low")
    s1 = await _story(session, film.id, "https://mshale.com/a")
    session.add(
        SourceDomain(
            domain="mshale.com",
            llm_tier="low",
            admin_override="none",
            first_seen_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.flush()
    await _run(session, film.id, [s1.id], confidence="confirmed")
    event = (await session.execute(select(Event).where(Event.film_id == film.id))).scalar_one()
    assert event.confidence == "rumored"


async def test_one_trusted_source_keeps_confirmed(session):
    film = await _film(session, "g-mix")
    s1 = await _story(session, film.id, "https://mshale.com/a")
    s2 = await _story(session, film.id, "https://variety.com/b")
    now = datetime.now(UTC)
    session.add_all(
        [
            SourceDomain(
                domain="mshale.com",
                llm_tier="low",
                admin_override="none",
                first_seen_at=now,
                updated_at=now,
            ),
            SourceDomain(
                domain="variety.com",
                llm_tier="trusted",
                admin_override="none",
                first_seen_at=now,
                updated_at=now,
            ),
        ]
    )
    await session.flush()
    await _run(session, film.id, [s1.id, s2.id], confidence="confirmed")
    event = (await session.execute(select(Event).where(Event.film_id == film.id))).scalar_one()
    assert event.confidence == "confirmed"


async def test_unknown_domain_uses_neutral_default_keeps_confirmed(session):
    film = await _film(session, "g-unknown")
    s1 = await _story(session, film.id, "https://newsite.example/a")
    await session.flush()
    await _run(session, film.id, [s1.id], confidence="confirmed")
    event = (await session.execute(select(Event).where(Event.film_id == film.id))).scalar_one()
    assert event.confidence == "confirmed"
