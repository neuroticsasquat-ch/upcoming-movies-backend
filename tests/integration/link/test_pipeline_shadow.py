"""Shadow mode in the `link` pipeline (NEU-996): retrieval runs beside the roster path,
records what it *would* have offered, and never gets to fail the run."""

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
    """Links every story to roster index 1 and clusters whatever it is given — the roster
    path deciding exactly as it does today, which is what shadow runs beside."""

    async def complete_call(self, *, model, system, messages, max_tokens=4096, calls):
        if "entity-linking classifier" in system[0]["text"]:
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


async def _story(session, url, *, title):
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


async def _catalog(session, *, stories: dict[str, str]) -> Film:
    """One tracked film plus `stories` as {url: headline}, all pending."""
    film = Film(tmdb_id=1, title="Runner")
    session.add(film)
    await session.flush()
    for url, title in stories.items():
        await _story(session, url, title=title)
    await session.commit()
    return film


async def _run(session, **kwargs):
    run_id = await create_run(session, kind="link")
    await session.commit()
    await run_link_ingest(
        session_factory=lambda: session,
        client=_FakeClient(),
        run_id=run_id,
        model="claude-haiku-4-5",
        cluster_model="claude-sonnet-4-6",
        recency_days=45,
        batch_size=10,
        floor=0.7,
        **kwargs,
    )
    return run_id


async def _probes(session, run_id) -> list[LinkRetrievalProbe]:
    return list(
        (
            await session.execute(
                select(LinkRetrievalProbe).where(LinkRetrievalProbe.run_id == run_id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


async def _health(session, run_id) -> RunRetrievalHealth | None:
    return (
        await session.execute(
            select(RunRetrievalHealth).where(RunRetrievalHealth.run_id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one_or_none()


async def _run_row(session, run_id) -> IngestRun:
    return (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def _stories(session) -> dict[str, Story]:
    return {
        s.url: s
        for s in (
            await session.execute(select(Story), execution_options={"populate_existing": True})
        )
        .scalars()
        .all()
    }


class TestShadowMode:
    async def test_a_probe_records_where_retrieval_put_the_rosters_pick(self, session):
        film = await _catalog(session, stories={"https://e/hit": "Runner wraps filming"})

        run_id = await _run(session, retrieval_mode="shadow")

        probe = (await _probes(session, run_id))[0]
        assert (probe.film_id, probe.retrieved, probe.rank) == (film.id, True, 1)
        assert probe.score == pytest.approx(1.0)
        assert probe.candidate_count == 1

    async def test_a_pick_retrieval_missed_is_recorded_as_a_miss(self, session):
        # The disagreement the shadow period exists to surface — and one the raw rate must
        # not be read as truth for, since the roster also makes false positives.
        await _catalog(session, stories={"https://e/miss": "Unrelated television coverage"})

        run_id = await _run(session, retrieval_mode="shadow")

        probe = (await _probes(session, run_id))[0]
        assert (probe.retrieved, probe.rank, probe.score, probe.candidate_count) == (
            False,
            None,
            None,
            0,
        )

    async def test_the_health_row_counts_every_story_retrieval_ran_over(self, session):
        # Not just the linked ones: the zero-candidate stories are absent from the probe
        # table by design, so only this row carries the denominator.
        await _catalog(
            session,
            stories={
                "https://e/hit": "Runner wraps filming",
                "https://e/miss": "Unrelated television coverage",
            },
        )

        run_id = await _run(session, retrieval_mode="shadow")

        health = await _health(session, run_id)
        assert health is not None
        assert (health.stories_retrieved, health.zero_candidate_stories) == (2, 1)
        assert (health.saturated_stories, health.mean_candidates) == (0, pytest.approx(0.5))

    async def test_the_cap_is_counted_as_saturation(self, session):
        session.add_all([Film(tmdb_id=1, title="Runner"), Film(tmdb_id=2, title="Runner Two")])
        await session.flush()
        await _story(session, "https://e/hit", title="Runner news")
        await session.commit()

        run_id = await _run(session, retrieval_mode="shadow", retrieval_max_candidates=1)

        health = await _health(session, run_id)
        assert health is not None
        assert (health.saturated_stories, health.mean_candidates) == (1, pytest.approx(1.0))

    async def test_the_roster_still_decides(self, session):
        film = await _catalog(session, stories={"https://e/miss": "Unrelated television coverage"})

        await _run(session, retrieval_mode="shadow")

        # Retrieval would have offered nothing, and the story is linked anyway: shadow
        # observes, it does not vote.
        story = (await _stories(session))["https://e/miss"]
        assert (story.link_status, story.film_id) == ("linked", film.id)

    async def test_a_run_with_nothing_pending_still_records_that_shadow_ran(self, session):
        # An absent health row means shadow did not run at all; a zero row means it ran and
        # saw nothing. Worth keeping apart while reading a shadow period.
        await _catalog(session, stories={})

        run_id = await _run(session, retrieval_mode="shadow")

        health = await _health(session, run_id)
        assert health is not None
        assert (health.stories_retrieved, health.mean_candidates) == (0, None)


class TestOtherModes:
    async def test_off_observes_nothing(self, session):
        await _catalog(session, stories={"https://e/hit": "Runner wraps filming"})

        run_id = await _run(session)

        assert await _probes(session, run_id) == []
        assert await _health(session, run_id) is None


class TestShadowNeverFailsTheRun:
    async def test_a_retrieval_failure_costs_the_observation_not_the_story(
        self, session, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("retrieval is broken")

        monkeypatch.setattr("upmovies.link.shadow.select_candidates", boom)
        film = await _catalog(session, stories={"https://e/hit": "Runner wraps filming"})

        run_id = await _run(session, retrieval_mode="shadow")

        assert (await _run_row(session, run_id)).status == "succeeded"
        story = (await _stories(session))["https://e/hit"]
        assert (story.link_status, story.film_id) == ("linked", film.id)
        assert await _probes(session, run_id) == []

    async def test_an_index_that_cannot_be_built_leaves_the_roster_path_alone(
        self, session, monkeypatch
    ):
        async def boom(*args, **kwargs):
            raise RuntimeError("the catalog read failed")

        monkeypatch.setattr("upmovies.link.shadow.build_candidate_index", boom)
        film = await _catalog(session, stories={"https://e/hit": "Runner wraps filming"})

        run_id = await _run(session, retrieval_mode="shadow")

        assert (await _run_row(session, run_id)).status == "succeeded"
        story = (await _stories(session))["https://e/hit"]
        assert (story.link_status, story.film_id) == ("linked", film.id)
        assert await _health(session, run_id) is None

    async def test_a_health_write_failure_keeps_the_probes_it_already_wrote(
        self, session, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("the health write failed")

        monkeypatch.setattr("upmovies.link.retrieval.health.RunRetrievalHealth", boom)
        await _catalog(session, stories={"https://e/hit": "Runner wraps filming"})

        run_id = await _run(session, retrieval_mode="shadow")

        assert (await _run_row(session, run_id)).status == "succeeded"
        assert len(await _probes(session, run_id)) == 1
