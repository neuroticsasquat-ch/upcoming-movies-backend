"""The organisation resolve pass against real rows (EF-12): the candidate union both sources
feed, what lands on `news.story_entity`, the catalog row an accept owes, the kind-keyed cache
and the closed-set band.

The routing table itself is pinned in `tests/unit/link/resolve/test_org_scoring.py`, which
needs neither a database nor a network. What only a database can prove is here.
"""

import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from tests.fixtures.gateway import StubGateway
from upmovies.catalog.models import (
    Collection,
    FilmProductionCompany,
    Person,
    ProductionCompany,
)
from upmovies.ingest.models import IngestRun, RunLLMUsage
from upmovies.ingest.runs import create_run
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.link.resolve.org_pipeline import run_org_resolution
from upmovies.link.resolve.scoring import Thresholds
from upmovies.llm import OpenAICompatClient
from upmovies.llm.types import Usage
from upmovies.news.models import ResolutionCache, Story, StoryEntity

BASE = "https://api.themoviedb.org/3"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

RESOLVE_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
RESOLVE_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def _client() -> TMDBClient:
    return TMDBClient(
        base_url=BASE,
        api_key="test-key",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    )


def company_row(entity_id: int, name: str) -> dict:
    return {"id": entity_id, "name": name, "logo_path": None, "origin_country": "US"}


def collection_row(entity_id: int, name: str) -> dict:
    return {"id": entity_id, "name": name, "poster_path": None, "backdrop_path": None}


def _search(endpoint: str, query: str, hits: list[dict]) -> None:
    respx.get(f"{BASE}/search/{endpoint}", params={"query": query, "page": 1}).mock(
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


async def _mention(session, story, name: str, kind: str = "company", **overrides) -> StoryEntity:
    fields: dict = {
        "story_id": story.id,
        "kind": kind,
        "name_as_written": name,
        "features": {"title_mentioned": None, "event_type": "company_attached"},
        "prompt_version": "3",
    }
    fields.update(overrides)
    row = StoryEntity(**fields)
    session.add(row)
    await session.flush()
    return row


async def _attach_company(session, film, entity_id: int, name: str) -> None:
    session.add(ProductionCompany(id=entity_id, name=name))
    await session.flush()
    session.add(FilmProductionCompany(film_id=film.id, company_id=entity_id))
    await session.flush()


async def _run_id(session) -> UUID:
    run_id = await create_run(session, kind="link")
    await session.commit()
    return run_id


async def _resolve(session_factory, session, **overrides):
    run_id = await _run_id(session)
    async with _client() as client:
        return await run_org_resolution(
            session_factory=session_factory, client=client, run_id=run_id, now=NOW, **overrides
        )


def _tiebreak_answer(content: str) -> None:
    respx.post(RESOLVE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1785283200,
                "model": RESOLVE_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 900, "completion_tokens": 12, "total_tokens": 912},
            },
        )
    )


async def _resolve_with_tiebreak(session_factory, session, **overrides):
    client = OpenAICompatClient(provider="deepinfra", api_key="di-test")
    gateway = StubGateway(
        per_stage={"resolve": client}, per_stage_provider={"resolve": "deepinfra"}
    )
    try:
        return await _resolve(
            session_factory, session, gateway=gateway, resolve_model=RESOLVE_MODEL, **overrides
        )
    finally:
        await client.aclose()


# --- the candidate union -------------------------------------------------------------------


@respx.mock
async def test_accepts_the_company_on_the_film_over_its_namesake(session, session_factory):
    """Being on the film is the one corroborating feature, and it separates two namesakes."""
    film = await add_film(session, 550, title="Fight Club")
    await _attach_company(session, film, 3172, "Blumhouse Productions")
    story = await _story(session, film)
    mention = await _mention(session, story, "Blumhouse Productions")
    await session.commit()
    _search(
        "company",
        "Blumhouse Productions",
        [company_row(9999, "Blumhouse Productions"), company_row(3172, "Blumhouse Productions")],
    )

    result = await _resolve(session_factory, session)

    assert result.accepted == 1
    await session.refresh(mention)
    assert (mention.path, mention.entity_id) == ("accepted", 3172)


@respx.mock
async def test_the_films_own_collection_is_a_candidate(session, session_factory):
    session.add(Collection(id=400, name="John Wick Collection"))
    await session.flush()
    film = await add_film(session, 551, title="John Wick 5", collection_id=400)
    story = await _story(session, film, slug="jw5")
    mention = await _mention(session, story, "John Wick", kind="collection")
    await session.commit()
    _search("collection", "John Wick", [])

    await _resolve(session_factory, session)

    await session.refresh(mention)
    # `John Wick` matches `John Wick Collection` on the reduced tier and is attached, so the
    # franchise the film is already filed under wins with no search hit at all.
    assert (mention.path, mention.entity_id) == ("accepted", 400)


# --- what lands on the row -----------------------------------------------------------------


@respx.mock
async def test_the_extraction_context_survives_the_resolver(session, session_factory):
    """`features` is merged, never replaced: `event_type` is what NEU-1446 reads."""
    film = await add_film(session, 552)
    story = await _story(session, film, slug="merge")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    await _resolve(session_factory, session)

    await session.refresh(mention)
    assert mention.features is not None
    assert mention.features["event_type"] == "company_attached"
    assert mention.features["resolution"]["kind"] == "company"
    assert mention.resolved_at == NOW


@respx.mock
async def test_the_whole_rejected_shortlist_is_logged(session, session_factory):
    film = await add_film(session, 553)
    story = await _story(session, film, slug="shortlist")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse"), company_row(70, "Universal")])

    await _resolve(session_factory, session)

    await session.refresh(mention)
    assert mention.candidates is not None
    assert [c["entity_id"] for c in mention.candidates] == [3172, 70]
    assert mention.candidates[0]["kind"] == "company"


@respx.mock
async def test_confidence_is_capped_at_the_storys_link(session, session_factory):
    """INV-6: a resolution is never more certain than the link it rests on."""
    film = await add_film(session, 554)
    story = await _story(session, film, slug="capped", link_confidence=0.3)
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    await _resolve(session_factory, session)

    await session.refresh(mention)
    assert mention.confidence == 0.3


@respx.mock
async def test_a_name_tmdb_does_not_hold_is_not_in_tmdb(session, session_factory):
    film = await add_film(session, 555)
    story = await _story(session, film, slug="nonesuch")
    mention = await _mention(session, story, "Nonesuch Pictures")
    await session.commit()
    _search("company", "Nonesuch Pictures", [])

    result = await _resolve(session_factory, session)

    assert result.not_in_tmdb == 1
    await session.refresh(mention)
    assert (mention.path, mention.entity_id) == ("not_in_tmdb", None)


@respx.mock
async def test_a_name_nothing_matches_is_unlinked(session, session_factory):
    film = await add_film(session, 556)
    story = await _story(session, film, slug="unlinked")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(70, "Universal Pictures")])

    result = await _resolve(session_factory, session)

    assert result.unlinked == 1
    await session.refresh(mention)
    assert (mention.path, mention.entity_id) == ("unlinked", None)


# --- the catalog row an accept owes ---------------------------------------------------------


@respx.mock
async def test_an_accepted_company_the_catalog_never_held_is_written(session, session_factory):
    """NEU-1446 joins `entity_id` to `catalog.production_company`; an accepted id with no row
    there resolves to a card nobody can be shown."""
    film = await add_film(session, 557)
    story = await _story(session, film, slug="upsert")
    await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    await _resolve(session_factory, session)

    company = await session.get(ProductionCompany, 3172)
    assert company is not None and company.name == "Blumhouse"


@respx.mock
async def test_the_rejected_namesakes_are_not_written_to_the_catalog(session, session_factory):
    """Both tables are read by the public studio pages and by header search, so writing the
    namesakes this pass rejected would put them in front of users."""
    film = await add_film(session, 558)
    story = await _story(session, film, slug="rejects")
    await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse"), company_row(70, "Universal")])

    await _resolve(session_factory, session)

    assert await session.get(ProductionCompany, 70) is None


@respx.mock
async def test_an_accepted_collection_the_catalog_never_held_is_written(session, session_factory):
    film = await add_film(session, 559)
    story = await _story(session, film, slug="coll-upsert")
    await _mention(session, story, "John Wick", kind="collection")
    await session.commit()
    _search("collection", "John Wick", [collection_row(400, "John Wick Collection")])

    await _resolve(session_factory, session)

    collection = await session.get(Collection, 400)
    assert collection is not None and collection.name == "John Wick Collection"


# --- the cache -----------------------------------------------------------------------------


@respx.mock
async def test_an_accept_is_cached_under_its_kind(session, session_factory):
    film = await add_film(session, 560)
    story = await _story(session, film, slug="cache-write")
    await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    await _resolve(session_factory, session)

    cached = await session.get(ResolutionCache, ("deadline.com", "Blumhouse", film.id, "company"))
    assert cached is not None
    assert (cached.entity_id, cached.person_id) == (3172, None)


@respx.mock
async def test_a_cached_answer_skips_the_search_entirely(session, session_factory):
    """The whole point of the cache: a second trade naming the same studio costs no request."""
    film = await add_film(session, 561)
    session.add(
        ResolutionCache(
            source_domain="deadline.com",
            name_as_written="Blumhouse",
            film_id=film.id,
            kind="company",
            entity_id=3172,
            confidence=0.7,
            resolved_at=NOW,
        )
    )
    story = await _story(session, film, slug="cache-hit")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    route = _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    result = await _resolve(session_factory, session)

    assert result.cache_hits == 1
    assert respx.calls.call_count == 0
    await session.refresh(mention)
    assert (mention.path, mention.entity_id) == ("accepted", 3172)
    assert route is None or True


@respx.mock
async def test_a_person_cache_row_is_not_read_as_an_organisation(session, session_factory):
    """`kind` is in the key because the same name means different things per catalogue."""
    film = await add_film(session, 562)
    session.add(Person(id=1892, name="Blumhouse"))
    await session.flush()
    session.add(
        ResolutionCache(
            source_domain="deadline.com",
            name_as_written="Blumhouse",
            film_id=film.id,
            kind="person",
            person_id=1892,
            confidence=0.9,
            resolved_at=NOW,
        )
    )
    story = await _story(session, film, slug="kind-key")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    result = await _resolve(session_factory, session)

    assert result.cache_hits == 0
    await session.refresh(mention)
    assert mention.entity_id == 3172


# --- the backlog ---------------------------------------------------------------------------


@respx.mock
async def test_a_decided_mention_is_not_re_resolved(session, session_factory):
    film = await add_film(session, 563)
    story = await _story(session, film, slug="decided")
    await _mention(session, story, "Blumhouse", path="unlinked", resolved_at=NOW)
    await session.commit()

    result = await _resolve(session_factory, session)

    assert result.resolved == 0
    assert respx.calls.call_count == 0


@respx.mock
async def test_a_mention_on_a_rejected_story_is_never_selected(session, session_factory):
    """Resolution is film-scoped end to end, and a rejected story has lost its film."""
    film = await add_film(session, 564)
    story = await _story(session, film, slug="rejected", link_status="rejected", film_id=None)
    await _mention(session, story, "Blumhouse")
    await session.commit()

    result = await _resolve(session_factory, session)

    assert result.resolved == 0


@respx.mock
async def test_the_limit_bounds_the_pass_and_the_rest_waits(session, session_factory):
    film = await add_film(session, 565)
    story = await _story(session, film, slug="bounded")
    await _mention(session, story, "Blumhouse")
    await _mention(session, story, "Universal")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])
    _search("company", "Universal", [company_row(33, "Universal")])

    result = await _resolve(session_factory, session, limit=1)

    assert result.resolved == 1
    remaining = (
        (await session.execute(select(StoryEntity).where(StoryEntity.path.is_(None))))
        .scalars()
        .all()
    )
    assert len(remaining) == 1


@respx.mock
async def test_one_failing_mention_does_not_take_the_pass_with_it(session, session_factory):
    film = await add_film(session, 566)
    story = await _story(session, film, slug="isolated")
    boom = await _mention(session, story, "Boom")
    ok = await _mention(session, story, "Blumhouse")
    await session.commit()
    respx.get(f"{BASE}/search/company", params={"query": "Boom", "page": 1}).mock(
        return_value=httpx.Response(500)
    )
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    result = await _resolve(session_factory, session)

    assert (result.failed, result.accepted) == (1, 1)
    await session.refresh(boom)
    await session.refresh(ok)
    assert boom.path is None  # back on the next run's backlog
    assert ok.path == "accepted"


@respx.mock
async def test_the_pass_ticks_the_runs_heartbeat(session, session_factory):
    film = await add_film(session, 567)
    story = await _story(session, film, slug="heartbeat")
    await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])
    run_id = await _run_id(session)

    async with _client() as client:
        await run_org_resolution(
            session_factory=session_factory, client=client, run_id=run_id, now=NOW
        )

    run = await session.get(IngestRun, run_id, populate_existing=True)
    assert run is not None and run.items_processed == 1


# --- the band ------------------------------------------------------------------------------


@respx.mock
async def test_two_namesakes_with_no_gateway_rest_on_the_tiebreak_route(session, session_factory):
    film = await add_film(session, 568)
    story = await _story(session, film, slug="band")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search(
        "company", "Blumhouse", [company_row(3172, "Blumhouse"), company_row(9999, "Blumhouse")]
    )

    result = await _resolve(session_factory, session)

    assert result.tiebreak == 1
    await session.refresh(mention)
    assert (mention.path, mention.entity_id) == ("tiebreak", None)


@respx.mock
async def test_the_model_names_one_of_the_shortlist(session, session_factory):
    film = await add_film(session, 569)
    story = await _story(session, film, slug="band-answered")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search(
        "company", "Blumhouse", [company_row(3172, "Blumhouse"), company_row(9999, "Blumhouse")]
    )
    _tiebreak_answer(json.dumps({"option": 2, "reason": "the television arm is not meant"}))

    result = await _resolve_with_tiebreak(session_factory, session)

    assert (result.tiebreak_asked, result.tiebreak_decided) == (1, 1)
    await session.refresh(mention)
    # The route records *how* it was decided (D-25), so a model's answer inside the band stays
    # findable rather than being buried among the arithmetic's own accepts.
    assert mention.path == "tiebreak"
    assert mention.entity_id == 9999
    assert mention.features is not None
    assert mention.features["resolution"]["tiebreak"]["answer"] == 2


@respx.mock
async def test_an_explicit_none_is_unlinked(session, session_factory):
    film = await add_film(session, 570)
    story = await _story(session, film, slug="band-none")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search(
        "company", "Blumhouse", [company_row(3172, "Blumhouse"), company_row(9999, "Blumhouse")]
    )
    _tiebreak_answer(json.dumps({"option": None, "reason": "the story does not say which arm"}))

    result = await _resolve_with_tiebreak(session_factory, session)

    assert result.tiebreak_declined == 1
    await session.refresh(mention)
    assert (mention.path, mention.entity_id) == ("unlinked", None)


@respx.mock
async def test_an_out_of_list_answer_changes_nothing(session, session_factory):
    """Coercing a defective reply to its nearest plausible option is the one thing the closed
    set exists to forbid."""
    film = await add_film(session, 571)
    story = await _story(session, film, slug="band-rejected")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search(
        "company", "Blumhouse", [company_row(3172, "Blumhouse"), company_row(9999, "Blumhouse")]
    )
    _tiebreak_answer(json.dumps({"option": 7, "reason": "none of these"}))

    result = await _resolve_with_tiebreak(session_factory, session)

    assert result.tiebreak_rejected == 1
    await session.refresh(mention)
    assert (mention.path, mention.entity_id) == ("tiebreak", None)
    assert mention.features is not None
    assert mention.features["resolution"]["tiebreak"]["out_of_list"] is True


@respx.mock
async def test_a_tiebreak_answer_is_never_cached(session, session_factory):
    """Caching an unreviewed model judgement would spread it across every later story from
    that publisher, as an `accepted` hit that no longer looks like a tiebreak."""
    film = await add_film(session, 572)
    story = await _story(session, film, slug="band-uncached")
    await _mention(session, story, "Blumhouse")
    await session.commit()
    _search(
        "company", "Blumhouse", [company_row(3172, "Blumhouse"), company_row(9999, "Blumhouse")]
    )
    _tiebreak_answer(json.dumps({"option": 1, "reason": "the feature film arm"}))

    await _resolve_with_tiebreak(session_factory, session)

    rows = (await session.execute(select(ResolutionCache))).scalars().all()
    assert rows == []


@respx.mock
async def test_the_stages_one_usage_row_carries_both_arms(session, session_factory):
    """`record_llm_usage` overwrites the (run, stage) row rather than adding to it, and both
    arms share the `resolve` stage — so the second one to ask a tiebreak has to write the
    total or it would erase the person arm's cost from `/admin/runs`."""
    film = await add_film(session, 580)
    story = await _story(session, film, slug="carried-usage")
    await _mention(session, story, "Blumhouse")
    await session.commit()
    _search(
        "company", "Blumhouse", [company_row(3172, "Blumhouse"), company_row(9999, "Blumhouse")]
    )
    _tiebreak_answer(json.dumps({"option": 1, "reason": "the feature film arm"}))

    # 900 prompt tokens from the person arm, and the organisation arm's own call on top.
    await _resolve_with_tiebreak(session_factory, session, carried_usage=Usage(input_tokens=900))

    usage = (await session.execute(select(RunLLMUsage))).scalars().one()
    assert (usage.stage, usage.input_tokens) == ("resolve", 1800)


# --- the run's detail clause ----------------------------------------------------------------


@respx.mock
async def test_the_detail_clause_names_organisations(session, session_factory):
    film = await add_film(session, 573)
    story = await _story(session, film, slug="detail")
    await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    result = await _resolve(session_factory, session)

    detail = result.detail("organisations")
    assert detail is not None
    assert "resolved 1 organisations" in detail


async def test_an_empty_backlog_says_nothing(session, session_factory):
    result = await _resolve(session_factory, session)
    assert result.detail("organisations") is None


@respx.mock
async def test_the_thresholds_are_threaded_through(session, session_factory):
    film = await add_film(session, 574)
    story = await _story(session, film, slug="thresholds")
    mention = await _mention(session, story, "Blumhouse")
    await session.commit()
    _search("company", "Blumhouse", [company_row(3172, "Blumhouse")])

    await _resolve(session_factory, session, thresholds=Thresholds(accept_floor=0.99))

    await session.refresh(mention)
    assert mention.path == "unlinked"
