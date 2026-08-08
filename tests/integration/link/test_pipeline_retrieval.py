"""The retrieval link path in the `link` pipeline (NEU-999) — since NEU-1004 the *only*
link path. It decides from each story's own candidate set, rejects the zero-candidate
majority without a model call, and keeps those rejects out of `StageCounts.processed` so the
one guard on the one lossy stage survives an outage."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.gateway import StubGateway
from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun, LinkRetrievalProbe, RunRetrievalHealth
from upmovies.ingest.runs import create_run
from upmovies.link.pipeline import run_link_ingest
from upmovies.llm import CallResult, Prompt
from upmovies.news.models import Story


class _FakeClient:
    """Links every story to its own candidate 1 and clusters whatever it is given, recording
    each link request so a test can assert what the model was — and was not — shown."""

    def __init__(self):
        self.link_requests: list[Prompt] = []

    async def complete_call(self, *, model, prompt, calls):
        if "entity-linking classifier" in prompt.stable_prefix:
            self.link_requests.append(prompt)
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

    async def complete_call(self, *, model, prompt, calls):
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
        gateway=StubGateway(client or _FakeClient()),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=45,
        batch_size=batch_size,
        floor=0.7,
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
        # The point of the whole project: the catalog leaves the stable prefix, so the
        # prefix stops scaling with catalog size (spec §4.2).
        await _catalog(
            session, films={1: "Runner"}, stories={"https://e/hit": "Runner wraps filming"}
        )
        client = _FakeClient()

        await _run(session, client=client)

        prompt = client.link_requests[0]
        assert "ROSTER" not in prompt.stable_prefix
        story = json.loads(prompt.user)["stories"][0]
        assert [c["title"] for c in story["candidates"]] == ["Runner"]

    async def test_only_the_storys_own_candidates_are_offered(self, session):
        await _catalog(
            session,
            films={1: "Runner", 2: "Sunup"},
            stories={"https://e/hit": "Runner wraps filming"},
        )
        client = _FakeClient()

        await _run(session, client=client)

        story = json.loads(client.link_requests[0].user)["stories"][0]
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

        sent = json.loads(client.link_requests[0].user)["stories"]
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
        breach's job — two different failures, two different guards (see `TestHardBreach`)."""
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


class TestHardBreach:
    """The hard tier of the two-tier guard (ADR-0010, NEU-1002): a zero-candidate rate past
    threshold finalizes the run `failed`, which is what makes `run_daily` abort and ping the
    deadman `/fail` — the pipeline's only alerting channel."""

    async def test_a_breaching_rate_fails_the_run(self, session):
        await _catalog(
            session,
            films={1: "Runner"},
            stories={
                "https://e/hit": "Runner wraps filming",
                "https://e/miss-a": "Unrelated television coverage",
                "https://e/miss-b": "A games console retrospective",
            },
        )

        run_id = await _run(
            session, retrieval_max_zero_candidate_rate=0.5, retrieval_health_min_stories=3
        )

        run = await _run_row(session, run_id)
        assert run.status == "failed"
        assert run.error and "no candidates" in run.error

    async def test_the_stories_it_did_link_still_stand(self, session):
        """A breach is an alert, not a rollback. `link` is lossy, so a run undone after the
        fact would re-run over an empty backlog — the links that committed are the best
        outcome available, and the `failed` status is what stops the chain publishing on top
        of them."""
        films = await _catalog(
            session,
            films={1: "Runner"},
            stories={
                "https://e/hit": "Runner wraps filming",
                "https://e/miss-a": "Unrelated television coverage",
                "https://e/miss-b": "A games console retrospective",
            },
        )

        await _run(session, retrieval_max_zero_candidate_rate=0.5, retrieval_health_min_stories=3)

        rows = await _stories(session)
        assert (rows["https://e/hit"].link_status, rows["https://e/hit"].film_id) == (
            "linked",
            films["Runner"].id,
        )
        assert rows["https://e/miss-a"].link_note == "no-candidates"

    async def test_a_thin_backlog_does_not_fail_the_chain(self, session):
        """The minimum-denominator rule. Every one of these stories is a zero-candidate
        reject — a 100% rate — and the run still succeeds, because three stories cannot tell
        a retrieval collapse from a quiet news day."""
        await _catalog(
            session,
            films={1: "Runner"},
            stories={
                "https://e/miss-a": "Unrelated television coverage",
                "https://e/miss-b": "A games console retrospective",
            },
        )

        run_id = await _run(
            session, retrieval_max_zero_candidate_rate=0.25, retrieval_health_min_stories=50
        )

        assert (await _run_row(session, run_id)).status == "succeeded"

    async def test_a_healthy_run_is_untouched(self, session):
        await _catalog(
            session, films={1: "Runner"}, stories={"https://e/hit": "Runner wraps filming"}
        )

        run_id = await _run(
            session, retrieval_max_zero_candidate_rate=0.25, retrieval_health_min_stories=1
        )

        assert (await _run_row(session, run_id)).status == "succeeded"

    async def test_an_outage_and_a_breach_are_both_reported(self, session):
        """Two guards, kept separate (spec §8): `total_failure` still watches model
        availability on its own narrow "produced nothing at all" rule, and retrieval health
        watches the rate. A run can trip both, and the error says so."""
        await _catalog(
            session,
            films={1: "Runner"},
            stories={
                "https://e/hit": "Runner wraps filming",
                "https://e/miss-a": "Unrelated television coverage",
                "https://e/miss-b": "A games console retrospective",
            },
        )

        run_id = await _run(
            session,
            client=_OutageClient(),
            retrieval_max_zero_candidate_rate=0.5,
            retrieval_health_min_stories=3,
        )

        run = await _run_row(session, run_id)
        assert run.status == "failed"
        assert run.error and "link stage produced nothing" in run.error
        assert "no candidates" in run.error
