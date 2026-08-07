"""The retrieval link path in the `link` pipeline (NEU-999): `LINK_RETRIEVAL_MODE=on`
decides from each story's own candidate set, rejects the zero-candidate majority without a
model call, and keeps those rejects out of `StageCounts.processed` so the one guard on the
one lossy stage survives an outage."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun, LinkRetrievalProbe, RunRetrievalHealth
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm.client import CallResult
from upmovies.news.models import Story


class _FakeClient:
    """Links every story to its own candidate 1 and clusters whatever it is given, recording
    each link request so a test can assert what the model was — and was not — shown."""

    def __init__(self):
        self.link_requests: list[dict] = []

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        if "entity-linking classifier" in system[0]["text"]:
            self.link_requests.append({"system": system, "messages": messages})
            stories = json.loads(messages[0]["content"])["stories"]
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
        new_ns = [s["n"] for s in json.loads(messages[0]["content"])["new_stories"]]
        return calls.record(
            CallResult(
                text=json.dumps(
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
            )
        )


class _OutageClient(_FakeClient):
    """Every model call raises — a total Anthropic outage. Retrieval still runs: it is pure
    and needs no network, which is exactly what makes this failure mode reachable."""

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        raise RuntimeError("boom")


async def _catalog(session, *, films: dict[int, str], stories: dict[str, str]) -> dict[str, Film]:
    """`films` as {tmdb_id: title} and `stories` as {url: headline}, all pending."""
    by_title = {title: Film(tmdb_id=tmdb_id, title=title) for tmdb_id, title in films.items()}
    session.add_all(by_title.values())
    await session.flush()
    for url, title in stories.items():
        session.add(
            Story(
                source="X",
                url=url,
                title=title,
                published_at=datetime.now(UTC) - timedelta(days=1),
                link_status="pending",
                raw={"summary": ""},
            )
        )
    await session.commit()
    return by_title


async def _run(session, *, client=None, batch_size=10, **kwargs):
    run_id = await create_run(session, kind="link")
    await session.commit()
    await run_link_ingest(
        session_factory=lambda: session,
        client=client or _FakeClient(),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=45,
        batch_size=batch_size,
        floor=0.7,
        retrieval_mode="on",
        **kwargs,
    )
    return run_id


async def _stories(session) -> dict[str, Story]:
    return {
        s.url: s
        for s in (
            await session.execute(select(Story), execution_options={"populate_existing": True})
        )
        .scalars()
        .all()
    }


async def _run_row(session, run_id) -> IngestRun:
    return (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def _health(session, run_id) -> RunRetrievalHealth | None:
    return (
        await session.execute(
            select(RunRetrievalHealth).where(RunRetrievalHealth.run_id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one_or_none()


class TestRetrievalDecides:
    async def test_a_retrieved_film_is_linked_from_the_storys_own_candidates(self, session):
        films = await _catalog(
            session, films={1: "Runner"}, stories={"https://e/hit": "Runner wraps filming"}
        )

        await _run(session)

        story = (await _stories(session))["https://e/hit"]
        assert (story.link_status, story.film_id) == ("linked", films["Runner"].id)

    async def test_the_prompt_carries_candidates_instead_of_a_roster(self, session):
        # The point of the whole project: the catalog leaves the system block, so the prefix
        # stops scaling with catalog size (spec §4.2).
        await _catalog(
            session, films={1: "Runner"}, stories={"https://e/hit": "Runner wraps filming"}
        )
        client = _FakeClient()

        await _run(session, client=client)

        system = client.link_requests[0]["system"]
        assert "ROSTER" not in system[0]["text"]
        assert "cache_control" not in system[0]
        story = json.loads(client.link_requests[0]["messages"][0]["content"])["stories"][0]
        assert [c["title"] for c in story["candidates"]] == ["Runner"]

    async def test_only_the_storys_own_candidates_are_offered(self, session):
        await _catalog(
            session,
            films={1: "Runner", 2: "Sunup"},
            stories={"https://e/hit": "Runner wraps filming"},
        )
        client = _FakeClient()

        await _run(session, client=client)

        story = json.loads(client.link_requests[0]["messages"][0]["content"])["stories"][0]
        assert [c["title"] for c in story["candidates"]] == ["Runner"]


class TestZeroCandidateRejection:
    async def test_a_story_with_no_candidates_is_rejected_without_a_model_call(self, session):
        await _catalog(
            session,
            films={1: "Runner"},
            stories={"https://e/miss": "Unrelated television coverage"},
        )
        client = _FakeClient()

        await _run(session, client=client)

        story = (await _stories(session))["https://e/miss"]
        assert (story.link_status, story.link_note) == ("rejected", "no-candidates")
        assert story.film_id is None
        assert story.linked_at is not None
        # Where the cost saving lands: the majority of the stage's workload never calls out.
        assert client.link_requests == []

    async def test_a_mixed_batch_calls_only_for_the_stories_with_candidates(self, session):
        await _catalog(
            session,
            films={1: "Runner"},
            stories={
                "https://e/hit": "Runner wraps filming",
                "https://e/miss": "Unrelated television coverage",
            },
        )
        client = _FakeClient()

        await _run(session, client=client)

        sent = json.loads(client.link_requests[0]["messages"][0]["content"])["stories"]
        assert len(sent) == 1
        rows = await _stories(session)
        assert rows["https://e/hit"].link_status == "linked"
        assert rows["https://e/miss"].link_note == "no-candidates"


class TestZeroCandidateRejectsAreNotProcessed:
    async def test_an_outage_still_fails_the_run_when_most_stories_had_no_candidates(self, session):
        """The regression this ticket exists to prevent. `StageCounts.total_failure` returns
        `False` the instant `processed > 0`, so counting zero-candidate rejects as processed
        would let a total Anthropic outage finalize `succeeded` — and `link` being lossy, the
        failed stories would age out and be gone (ADR-0009)."""
        await _catalog(
            session,
            films={1: "Runner"},
            stories={
                "https://e/hit": "Runner wraps filming",
                "https://e/miss-a": "Unrelated television coverage",
                "https://e/miss-b": "A games console retrospective",
            },
        )

        run_id = await _run(session, client=_OutageClient())

        rows = await _stories(session)
        # The rejects stand: no model decided them, so no model outage can undo them.
        assert rows["https://e/miss-a"].link_note == "no-candidates"
        assert rows["https://e/miss-b"].link_note == "no-candidates"
        # The story that needed the classifier stays pending for a later run to retry.
        assert rows["https://e/hit"].link_status == "pending"

        run = await _run_row(session, run_id)
        assert run.status == "failed"
        assert run.error and "link stage" in run.error

    async def test_a_run_whose_stories_all_had_candidates_still_fails_on_an_outage(self, session):
        await _catalog(
            session, films={1: "Runner"}, stories={"https://e/hit": "Runner wraps filming"}
        )

        run_id = await _run(session, client=_OutageClient())

        assert (await _run_row(session, run_id)).status == "failed"

    async def test_a_run_of_nothing_but_zero_candidate_stories_is_not_an_outage(self, session):
        """0 processed / 0 failed is an idempotent no-op, which `StageCounts` reads correctly.
        An index build that broke and zeroed every story is the *retrieval-health* hard
        breach's job (M4) — two different failures, two different guards."""
        await _catalog(
            session,
            films={1: "Runner"},
            stories={"https://e/miss": "Unrelated television coverage"},
        )

        run_id = await _run(session)

        assert (await _run_row(session, run_id)).status == "succeeded"


class TestRetrievalHealth:
    async def test_the_health_row_counts_every_story_retrieval_ran_over(self, session):
        await _catalog(
            session,
            films={1: "Runner"},
            stories={
                "https://e/hit": "Runner wraps filming",
                "https://e/miss": "Unrelated television coverage",
            },
        )

        run_id = await _run(session)

        health = await _health(session, run_id)
        assert health is not None
        assert (health.stories_retrieved, health.zero_candidate_stories) == (2, 1)
        assert health.mean_candidates == pytest.approx(0.5)

    async def test_it_writes_no_probe_rows(self, session):
        # A probe adjudicates retrieval against a *roster* pick. With the roster gone there
        # is no second opinion to compare against, so the table stays shadow-only.
        await _catalog(
            session, films={1: "Runner"}, stories={"https://e/hit": "Runner wraps filming"}
        )

        run_id = await _run(session)

        probes = (
            (
                await session.execute(
                    select(LinkRetrievalProbe).where(LinkRetrievalProbe.run_id == run_id),
                    execution_options={"populate_existing": True},
                )
            )
            .scalars()
            .all()
        )
        assert list(probes) == []

    async def test_health_is_recorded_even_when_the_run_had_nothing_pending(self, session):
        await _catalog(session, films={1: "Runner"}, stories={})

        run_id = await _run(session)

        health = await _health(session, run_id)
        assert health is not None
        assert (health.stories_retrieved, health.mean_candidates) == (0, None)
