"""The resolve pass against real rows: the cache, what lands on `story_person`, the person
upsert an accept owes, and the counters the link run reports.

The routing table itself is pinned in `tests/unit/link/resolve/test_scoring.py`, which needs
neither a database nor a network. What only a database can prove is here.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_credit, add_film
from tests.fixtures.gateway import StubGateway
from tests.fixtures.tmdb import make_person_search_hit
from upmovies.catalog.models import Person
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.link.pipeline import run_link_ingest
from upmovies.link.resolve.pipeline import run_resolution
from upmovies.link.resolve.scoring import Thresholds
from upmovies.llm import CallResult
from upmovies.news.models import ResolutionCache, Story, StoryPerson

BASE = "https://api.themoviedb.org/3"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _client() -> TMDBClient:
    return TMDBClient(
        base_url=BASE,
        api_key="test-key",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    )


def _search(query: str, hits: list[dict]) -> None:
    respx.get(f"{BASE}/search/person", params={"query": query, "page": 1}).mock(
        return_value=httpx.Response(
            200, json={"page": 1, "results": hits, "total_pages": 1, "total_results": len(hits)}
        )
    )


async def _story(session, film, **overrides) -> Story:
    fields: dict = {
        "source": "deadline",
        "url": f"https://deadline.com/{overrides.pop('slug', 'a-story')}",
        "title": "A trade story",
        "film_id": film.id,
        "link_status": "linked",
        "link_confidence": 0.95,
    }
    fields.update(overrides)
    story = Story(**fields)
    session.add(story)
    await session.flush()
    return story


async def _mention(session, story, name: str, **overrides) -> StoryPerson:
    fields: dict = {
        "story_id": story.id,
        "name_as_written": name,
        "features": {"title_mentioned": None, "event_type": "casting"},
        "prompt_version": "2",
    }
    fields.update(overrides)
    row = StoryPerson(**fields)
    session.add(row)
    await session.flush()
    return row


async def _run_id(session) -> UUID:
    run_id = await create_run(session, kind="link")
    await session.commit()
    return run_id


def _features(row: StoryPerson) -> dict:
    assert row.features is not None
    return row.features


def _logged_candidates(row: StoryPerson) -> list:
    assert row.candidates is not None
    return row.candidates


async def _resolve(session_factory, session, **overrides):
    run_id = await _run_id(session)
    async with _client() as client:
        return await run_resolution(
            session_factory=session_factory,
            client=client,
            run_id=run_id,
            now=NOW,
            **overrides,
        )


@respx.mock
async def test_an_accept_writes_the_person_the_confidence_and_the_cache(session_factory, session):
    film = await add_film(session, 1)
    story = await _story(session, film)
    mention = await _mention(session, story, "Chris Evans")
    await session.commit()
    _search("Chris Evans", [make_person_search_hit(500, name="Chris Evans")])

    result = await _resolve(session_factory, session)

    assert (result.accepted, result.resolved) == (1, 1)
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (500, "accepted")
    assert mention.confidence is not None and mention.confidence <= 0.95
    assert mention.resolved_at == NOW
    # The search hit went through the shared person upsert, so the FK the row just took has
    # something to point at and the headshot came with it.
    person = await session.get(Person, 500)
    assert person is not None and person.profile_path == "/profile500.jpg"
    cached = await session.get(ResolutionCache, ("deadline.com", "Chris Evans", film.id))
    assert cached is not None and cached.person_id == 500


@respx.mock
async def test_a_cache_hit_skips_scoring_entirely(session_factory, session):
    """No TMDB request at all — which is the whole point of the cache, and is asserted as the
    absence of a route rather than as a count, because a mocked route that is never called is
    exactly what a skipped search looks like."""
    film = await add_film(session, 2)
    session.add(Person(id=501, name="Chris Evans"))
    story = await _story(session, film, slug="second-story")
    mention = await _mention(session, story, "Chris Evans")
    session.add(
        ResolutionCache(
            source_domain="deadline.com",
            name_as_written="Chris Evans",
            film_id=film.id,
            person_id=501,
            confidence=0.88,
            resolved_at=NOW - timedelta(days=1),
        )
    )
    await session.commit()

    result = await _resolve(session_factory, session)

    assert (result.cache_hits, result.accepted) == (1, 1)
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (501, "accepted")
    assert _features(mention)["resolution"]["cache_hit"] is True
    assert mention.candidates is None


@respx.mock
async def test_a_cache_hit_is_re_capped_against_this_story_link(session_factory, session):
    """INV-6 binds the resolution to the link it rests on, and the story that filled the
    cache is not the story being resolved now."""
    film = await add_film(session, 3)
    session.add(Person(id=502, name="Chris Evans"))
    story = await _story(session, film, slug="weak-link", link_confidence=0.6)
    mention = await _mention(session, story, "Chris Evans")
    session.add(
        ResolutionCache(
            source_domain="deadline.com",
            name_as_written="Chris Evans",
            film_id=film.id,
            person_id=502,
            confidence=0.92,
            resolved_at=NOW - timedelta(days=1),
        )
    )
    await session.commit()

    await _resolve(session_factory, session)

    await session.refresh(mention)
    assert mention.confidence == 0.6


@respx.mock
async def test_the_features_the_extraction_pass_wrote_survive_the_resolver(
    session_factory, session
):
    """`title_mentioned` and `event_type` are written once at clustering and never
    regenerated, and they are inputs to this pass — a resolver that assigned a fresh dict
    would destroy the only record of what the model saw."""
    film = await add_film(session, 4)
    story = await _story(session, film, slug="with-context")
    mention = await _mention(
        session,
        story,
        "Chris Evans",
        features={"title_mentioned": "Some Other Film", "event_type": "casting"},
    )
    await session.commit()
    _search("Chris Evans", [make_person_search_hit(503, name="Chris Evans")])

    await _resolve(session_factory, session)

    await session.refresh(mention)
    assert _features(mention)["title_mentioned"] == "Some Other Film"
    assert _features(mention)["event_type"] == "casting"
    assert _features(mention)["resolution"]["cache_hit"] is False
    assert _features(mention)["resolution"]["best_score"] > 0


@respx.mock
async def test_every_candidate_is_logged_with_its_features_not_only_the_winner(
    session_factory, session
):
    """D-25's queue is worth working because it shows the near-misses a decision rejected."""
    film = await add_film(session, 5)
    await add_credit(session, film, 600, credit_type="crew", job="Director")
    story = await _story(session, film, slug="shortlist")
    mention = await _mention(session, story, "Chris Evans")
    await session.commit()
    _search("Chris Evans", [make_person_search_hit(601, name="Chris Evans")])

    await _resolve(session_factory, session)

    await session.refresh(mention)
    logged = {c["person_id"]: c for c in _logged_candidates(mention)}
    assert set(logged) == {600, 601}
    assert logged[601]["features"]["name_match"] == "exact"
    assert logged[600]["features"]["name_match"] == "none"
    assert logged[600]["features"]["credited"] is True


@respx.mock
async def test_a_tie_between_namesakes_queues_a_tiebreak_and_names_nobody(session_factory, session):
    film = await add_film(session, 6)
    story = await _story(session, film, slug="two-chrises")
    mention = await _mention(session, story, "Chris Evans")
    await session.commit()
    _search(
        "Chris Evans",
        [
            make_person_search_hit(700, name="Chris Evans", popularity=90.0),
            make_person_search_hit(701, name="Chris Evans", popularity=1.0),
        ],
    )

    result = await _resolve(session_factory, session)

    assert result.tiebreak == 1
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (None, "tiebreak")
    # Nothing was accepted, so nothing was cached: the band is for the resolve stage to
    # decide, and caching an undecided mention would cache the indecision.
    assert await session.get(ResolutionCache, ("deadline.com", "Chris Evans", film.id)) is None
    # Popularity ordered the shortlist the resolve stage will read, and decided nothing else.
    assert [c["person_id"] for c in _logged_candidates(mention)] == [700, 701]


@respx.mock
async def test_an_empty_search_on_a_casting_beat_is_not_in_tmdb(session_factory, session):
    film = await add_film(session, 7)
    story = await _story(session, film, slug="a-debut")
    mention = await _mention(session, story, "Nobody Knownyet")
    await session.commit()
    _search("Nobody Knownyet", [])

    result = await _resolve(session_factory, session)

    assert result.not_in_tmdb == 1
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (None, "not_in_tmdb")


@respx.mock
async def test_a_mention_on_an_unlinked_story_is_never_selected(session_factory, session):
    """Resolution is film-scoped end to end, so a story the linker rejected — its `film_id`
    nulled — leaves its mentions nothing to resolve against."""
    film = await add_film(session, 8)
    rejected = await _story(session, film, slug="rejected", link_status="rejected", film_id=None)
    mention = await _mention(session, rejected, "Chris Evans")
    await session.commit()

    result = await _resolve(session_factory, session)

    assert result.resolved == 0
    await session.refresh(mention)
    assert mention.path is None


@respx.mock
async def test_a_resolved_mention_is_not_worked_twice(session_factory, session):
    film = await add_film(session, 9)
    story = await _story(session, film, slug="already-done")
    await _mention(session, story, "Chris Evans", path="unlinked", resolved_at=NOW)
    await session.commit()

    result = await _resolve(session_factory, session)

    assert (result.resolved, result.failed) == (0, 0)


@respx.mock
async def test_a_tmdb_failure_leaves_the_mention_in_the_backlog(session_factory, session):
    """The right resting state for a blip: nothing is lost by waiting, and a half-written
    decision would be a wrong answer only a human re-reading the queue would ever catch."""
    film = await add_film(session, 10)
    story = await _story(session, film, slug="tmdb-down")
    mention = await _mention(session, story, "Chris Evans")
    await session.commit()
    respx.get(f"{BASE}/search/person").mock(return_value=httpx.Response(500))

    result = await _resolve(session_factory, session)

    assert (result.failed, result.resolved) == (1, 0)
    await session.refresh(mention)
    assert (mention.path, mention.person_id, mention.resolved_at) == (None, None, None)


@respx.mock
async def test_the_per_run_limit_leaves_the_rest_as_next_runs_backlog(session_factory, session):
    film = await add_film(session, 11)
    story = await _story(session, film, slug="many-names")
    for n in (1, 2, 3):
        await _mention(session, story, f"Person Number{n}")
    await session.commit()
    for n in (1, 2, 3):
        _search(f"Person Number{n}", [])

    result = await _resolve(session_factory, session, limit=2)

    assert result.resolved == 2
    remaining = (
        (await session.execute(select(StoryPerson).where(StoryPerson.path.is_(None))))
        .scalars()
        .all()
    )
    assert len(remaining) == 1


@respx.mock
async def test_thresholds_reach_the_pass_from_settings(session_factory, session):
    film = await add_film(session, 12)
    story = await _story(session, film, slug="wide-band")
    mention = await _mention(session, story, "Chris Evans")
    await session.commit()
    _search("Chris Evans", [make_person_search_hit(800, name="Chris Evans")])

    await _resolve(session_factory, session, thresholds=Thresholds(accept_floor=0.99))

    await session.refresh(mention)
    assert mention.path == "unlinked"
    assert _features(mention)["resolution"]["accept_floor"] == 0.99


class _ClusterOnlyClient:
    """Links every story to the one film and clusters them into one event, naming a person in
    the mention block the extraction contract asks for (D-20)."""

    async def complete_call(self, *, model, prompt, calls):
        if "entity-linking classifier" in prompt.stable_prefix:
            stories = json.loads(prompt.user)["stories"]
            return calls.record(
                CallResult(
                    text=json.dumps(
                        [
                            {"id": s["id"], "film": 1, "confidence": 0.95, "reason": "about"}
                            for s in stories
                        ]
                    )
                )
            )
        new_ns = [s["n"] for s in json.loads(prompt.user)["new_stories"]]
        return calls.record(
            CallResult(
                text=json.dumps(
                    {
                        "events": [
                            {
                                "existing": None,
                                "type": "casting",
                                "confidence": "confirmed",
                                "cast": ["Chris Evans"],
                                "stories": new_ns,
                            }
                        ],
                        "mentions": [
                            {
                                "n": n,
                                "name_as_written": "Chris Evans",
                                "role": "lead",
                                "department": "Acting",
                                "event_type": "casting",
                                "evidence_span": "Chris Evans will star",
                            }
                            for n in new_ns
                        ],
                    }
                )
            )
        )


@respx.mock
async def test_the_link_run_resolves_after_clustering_and_says_so_on_its_detail_line(
    session_factory, session
):
    """The wiring, end to end: clustering writes the mention and the same run resolves it —
    which is only possible in this order — and the counts ride the link run's own detail
    line rather than a second row on `/admin/runs`."""
    film = await add_film(session, 1, title="Film One")
    # A headline sharing the film's title tokens, so candidate retrieval reaches the linker
    # at all — resolution is downstream of a story that actually linked.
    await _story(
        session,
        film,
        slug="fresh",
        title="Film One casts Chris Evans",
        link_status="pending",
        link_confidence=None,
    )
    run_id = await create_run(session, kind="link")
    await session.commit()
    _search("Chris Evans", [make_person_search_hit(900, name="Chris Evans")])

    async with _client() as tmdb_client:
        await run_link_ingest(
            session_factory=session_factory,
            gateway=StubGateway(_ClusterOnlyClient()),
            run_id=run_id,
            model="claude-haiku-4-5",
            cluster_model="claude-sonnet-4-6",
            recency_days=30,
            batch_size=20,
            floor=0.7,
            tmdb_client=tmdb_client,
        )

    run = await session.get(IngestRun, run_id, execution_options={"populate_existing": True})
    assert run is not None
    assert "resolved 1 mentions (1 accepted" in (run.detail or "")
    mention = (await session.execute(select(StoryPerson))).scalars().one()
    assert (mention.person_id, mention.path) == (900, "accepted")


@respx.mock
async def test_a_link_run_with_no_tmdb_client_resolves_nothing_and_keeps_its_detail_line(
    session_factory, session
):
    """`RESOLVE_ENABLED=false` hands the pipeline no client, and the skip has to be a genuine
    one: the mention stays in the backlog rather than being worked and discarded."""
    film = await add_film(session, 1, title="Film One")
    # A headline sharing the film's title tokens, so candidate retrieval reaches the linker
    # at all — resolution is downstream of a story that actually linked.
    await _story(
        session,
        film,
        slug="fresh",
        title="Film One casts Chris Evans",
        link_status="pending",
        link_confidence=None,
    )
    run_id = await create_run(session, kind="link")
    await session.commit()

    await run_link_ingest(
        session_factory=session_factory,
        gateway=StubGateway(_ClusterOnlyClient()),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=30,
        batch_size=20,
        floor=0.7,
    )

    run = await session.get(IngestRun, run_id, execution_options={"populate_existing": True})
    assert run is not None
    assert "resolved" not in (run.detail or "")
    mention = (await session.execute(select(StoryPerson))).scalars().one()
    assert mention.path is None
