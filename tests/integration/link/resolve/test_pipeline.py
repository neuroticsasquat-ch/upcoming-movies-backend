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
from upmovies.ingest.models import IngestRun, LLMCall, RunLLMUsage
from upmovies.ingest.runs import create_run
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.link.pipeline import run_link_ingest
from upmovies.link.resolve.pipeline import run_resolution
from upmovies.link.resolve.scoring import Thresholds
from upmovies.llm import CallResult, OpenAICompatClient
from upmovies.news.models import ResolutionCache, Story, StoryPerson

BASE = "https://api.themoviedb.org/3"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

# The `resolve` stage, on the one provider a test can put a real adapter in front of: the
# Anthropic SDK moved to httpx2 and respx only intercepts httpx (`tests/unit/llm/conftest.py`).
# A DeepInfra-hosted model keeps the whole path real — `Prompt` serialization, the wire request,
# the usage mapping and the pricing key — where a fake `Completer` would only exercise this
# module's own half of it.
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


def _tiebreak_answer(content: str, *, prompt_tokens: int = 900) -> None:
    """Mock the `resolve` stage's provider with one closed-set answer."""
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
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 12,
                    "total_tokens": prompt_tokens + 12,
                },
            },
        )
    )


def _resolve_gateway() -> tuple[StubGateway, OpenAICompatClient]:
    client = OpenAICompatClient(provider="deepinfra", api_key="di-test")
    gateway = StubGateway(
        per_stage={"resolve": client},
        per_stage_provider={"resolve": "deepinfra"},
    )
    return gateway, client


async def _resolve_with_tiebreak(session_factory, session, **overrides):
    gateway, client = _resolve_gateway()
    try:
        return await _resolve(
            session_factory,
            session,
            gateway=gateway,
            resolve_model=RESOLVE_MODEL,
            **overrides,
        )
    finally:
        await client.aclose()


async def _two_namesakes(session, tmdb_id: int, slug: str):
    """A mention TMDB answers with two equally-scoring people — the band, by construction."""
    film = await add_film(session, tmdb_id, title="The Housemaid's Secret")
    story = await _story(session, film, slug=slug, title="Chris Evans joins the thriller")
    mention = await _mention(session, story, "Chris Evans")
    await session.commit()
    _search(
        "Chris Evans",
        [
            make_person_search_hit(700, name="Chris Evans", popularity=90.0),
            make_person_search_hit(701, name="Chris Evans", popularity=1.0),
        ],
    )
    return film, story, mention


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


# --- the resolve stage: the closed-set tiebreak (D-22, NEU-1364) -----------------
#
# The band the pass above queues, consumed. The prompt and the reply grammar are pinned in
# `tests/unit/link/resolve/test_tiebreak.py`; what only a database and a wire can prove is
# what lands on the row, what the telemetry says it cost, and that a defective answer changes
# nothing.


@respx.mock
async def test_the_model_picking_an_option_names_that_person_on_the_tiebreak_route(
    session_factory, session
):
    """An option answer accepts in every sense downstream reads — `person_id`, `confidence`,
    the person upsert the FK owes — while `path` still records that a model decided it. D-25
    exists so a human can find exactly these afterwards, which spelling it `accepted` would
    make impossible."""
    _, _, mention = await _two_namesakes(session, 20, "tiebreak-decided")
    _tiebreak_answer('{"option": 2, "reason": "the composer, not the actor"}')

    result = await _resolve_with_tiebreak(session_factory, session)

    assert (result.tiebreak_asked, result.tiebreak_decided) == (1, 1)
    assert (result.tiebreak, result.accepted) == (1, 0)
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (701, "tiebreak")
    assert mention.confidence is not None and mention.confidence <= 0.95
    assert await session.get(Person, 701) is not None
    tiebreak = _features(mention)["resolution"]["tiebreak"]
    assert (tiebreak["answer"], tiebreak["person_id"]) == (2, 701)
    assert tiebreak["reason"] == "the composer, not the actor"


@respx.mock
async def test_a_tiebreak_accept_is_never_cached(session_factory, session):
    """The one resolution a human is meant to review. Caching it would spread a single
    unreviewed judgement across every later story from that publisher naming that name on that
    film — and read back as an `accepted` hit that no longer looks like a tiebreak at all."""
    film, _, _ = await _two_namesakes(session, 21, "tiebreak-uncached")
    _tiebreak_answer('{"option": 1}')

    await _resolve_with_tiebreak(session_factory, session)

    assert await session.get(ResolutionCache, ("deadline.com", "Chris Evans", film.id)) is None


@respx.mock
async def test_the_model_answering_none_unlinks_the_mention(session_factory, session):
    """The answer the prompt asks for most often, and the safe one: an unlinked mention never
    alerts, which is the failure mode this whole milestone exists to make impossible."""
    _, _, mention = await _two_namesakes(session, 22, "tiebreak-none")
    _tiebreak_answer('{"option": null, "reason": "neither is on this film"}')

    result = await _resolve_with_tiebreak(session_factory, session)

    assert (result.tiebreak_declined, result.unlinked, result.tiebreak) == (1, 1, 0)
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (None, "unlinked")
    assert _features(mention)["resolution"]["tiebreak"]["answer"] is None


@respx.mock
async def test_an_out_of_list_answer_leaves_the_deterministic_decision_standing(
    session_factory, session
):
    """Rejected, not coerced — the link stage's rule. The mention stays in the band with
    nobody named, on the queue a human works, and is not asked again: the route is written, so
    it leaves the backlog rather than buying a call every night forever."""
    _, _, mention = await _two_namesakes(session, 23, "tiebreak-out-of-list")
    _tiebreak_answer('{"option": 9}')

    result = await _resolve_with_tiebreak(session_factory, session)

    assert (result.tiebreak_rejected, result.tiebreak_decided) == (1, 0)
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (None, "tiebreak")
    assert _features(mention)["resolution"]["tiebreak"]["out_of_list"] is True
    assert mention.resolved_at == NOW


@respx.mock
async def test_an_unparseable_answer_leaves_the_deterministic_decision_standing(
    session_factory, session
):
    _, _, mention = await _two_namesakes(session, 24, "tiebreak-garbage")
    _tiebreak_answer("I really could not say")

    result = await _resolve_with_tiebreak(session_factory, session)

    assert result.tiebreak_rejected == 1
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (None, "tiebreak")
    assert _features(mention)["resolution"]["tiebreak"]["unparseable"] is True


@respx.mock
async def test_the_call_is_logged_like_every_other_stage(session_factory, session):
    """One `ingest.llm_call` row and one `ingest.run_llm_usage` row, both naming the provider
    that actually answered — the pricing key is `(provider, model)`, so a row naming the wrong
    host reports one provider's cost under another's name and no later analysis can tell."""
    await _two_namesakes(session, 25, "tiebreak-logged")
    _tiebreak_answer('{"option": 1}')

    await _resolve_with_tiebreak(session_factory, session)

    call = (await session.execute(select(LLMCall))).scalars().one()
    assert (call.stage, call.provider, call.model) == ("resolve", "deepinfra", RESOLVE_MODEL)
    assert (call.input_tokens, call.output_tokens) == (900, 12)
    assert call.parse_ok is True
    usage = (await session.execute(select(RunLLMUsage))).scalars().one()
    assert (usage.stage, usage.model) == ("resolve", RESOLVE_MODEL)
    assert usage.input_tokens == 900
    assert usage.cost_usd > 0


@respx.mock
async def test_a_failed_tiebreak_call_leaves_the_mention_in_the_backlog(session_factory, session):
    """A provider blip is the same class of thing as a TMDB blip and rests the same way: the
    mention keeps until the next run rather than being recorded as answered. The call is still
    a row — a failure nobody can price is a failure nobody can see."""
    _, _, mention = await _two_namesakes(session, 26, "tiebreak-down")
    respx.post(RESOLVE_URL).mock(return_value=httpx.Response(500))

    result = await _resolve_with_tiebreak(session_factory, session)

    assert (result.failed, result.resolved) == (1, 0)
    await session.refresh(mention)
    assert (mention.path, mention.resolved_at) == (None, None)
    call = (await session.execute(select(LLMCall))).scalars().one()
    assert (call.stage, call.ok) == ("resolve", False)


@respx.mock
async def test_the_band_is_left_alone_when_no_gateway_is_supplied(session_factory, session):
    """Every deterministic decision still lands — the pass is not gated on having a model,
    only the band is."""
    _, _, mention = await _two_namesakes(session, 27, "no-gateway")

    result = await _resolve(session_factory, session)

    assert (result.tiebreak, result.tiebreak_asked) == (1, 0)
    await session.refresh(mention)
    assert (mention.person_id, mention.path) == (None, "tiebreak")
    assert "tiebreak" not in _features(mention)["resolution"]


@respx.mock
async def test_the_detail_line_reports_the_band_only_when_one_was_asked(session_factory, session):
    """A "0 asked" on every run's line is one the eye stops seeing — the same reason the link
    run's saturation note is a clause it only sometimes carries."""
    await _two_namesakes(session, 28, "detail-line")
    _tiebreak_answer('{"option": 1}')

    asked = await _resolve_with_tiebreak(session_factory, session)
    assert "1 tiebreaks asked (1 decided, 0 none, 0 rejected)" in (asked.detail() or "")

    quiet = await _resolve(session_factory, session)
    assert quiet.detail() is None


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
