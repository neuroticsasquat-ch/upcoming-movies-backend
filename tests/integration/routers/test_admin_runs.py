"""Session + is_admin protected, read-only ingest-run endpoints for the admin UI.
Distinct from the ADMIN_TOKEN trigger/poll endpoints."""

import itertools
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun, LinkRetrievalProbe, RunRetrievalHealth
from upmovies.ingest.sweep import (
    CreditDetachmentResult,
    CreditEventResult,
    EnumerateResult,
    FieldEventResult,
    RefreshResult,
    ReleaseEventResult,
    sweep_detail,
)
from upmovies.main import app
from upmovies.news.models import Story

# `catalog.film.tmdb_id` is unique, and these tests seed several films per run.
_tmdb_ids = itertools.count(90_000)


@pytest.fixture
async def anon_client(session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


async def _seed_runs(session) -> tuple[IngestRun, IngestRun]:
    tmdb = IngestRun(kind="tmdb", status="succeeded", items_processed=5, items_failed=1)
    feeds = IngestRun(kind="feeds", status="running")
    session.add_all([tmdb, feeds])
    await session.commit()
    await session.refresh(tmdb)
    await session.refresh(feeds)
    return tmdb, feeds


# --- gating --------------------------------------------------------------------


async def test_list_runs_requires_authentication(anon_client):
    assert (await anon_client.get("/admin/runs")).status_code == 401


async def test_list_runs_forbidden_for_non_admin(authed_client):
    assert (await authed_client.get("/admin/runs")).status_code == 403


async def test_run_detail_forbidden_for_non_admin(authed_client, session):
    tmdb, _ = await _seed_runs(session)
    assert (await authed_client.get(f"/admin/runs/{tmdb.id}")).status_code == 403


# --- admin reads ---------------------------------------------------------------


async def test_admin_lists_recent_runs(admin_authed_client, session):
    await _seed_runs(session)
    r = await admin_authed_client.get("/admin/runs")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert {row["kind"] for row in body} == {"tmdb", "feeds"}


async def test_admin_lists_the_sweep_with_every_phase_counter(admin_authed_client, session):
    """The sweep's whole reason for having its own run kind (spec §6.1): a legible row of its
    own rather than counters hidden inside the tmdb stage's. `detail` is the only place the
    five phases are told apart, and a run that enumerated fine and refreshed nothing — or
    refreshed fine and carded nothing — is the failure it exists to make visible (§6.2)."""
    detail = sweep_detail(
        EnumerateResult(
            seed_people=7519,
            candidates_found=42,
            admitted=0,
            withheld=30,
            skipped_below_corroboration=12,
        ),
        RefreshResult(selected=300, refreshed=299, dormant_selected=12, failures=1),
        FieldEventResult(changes_read=58, events_created=6, skipped=52),
        CreditEventResult(attachments_read=19, events_created=5, skipped=14),
        CreditDetachmentResult(),
        ReleaseEventResult(changes_read=11, events_created=3, skipped=8),
    )
    run = IngestRun(
        kind="sweep", status="succeeded", items_processed=341, items_failed=1, detail=detail
    )
    session.add(run)
    await session.commit()

    r = await admin_authed_client.get("/admin/runs")

    assert r.status_code == 200
    row = next(row for row in r.json() if row["kind"] == "sweep")
    assert (
        "enumerate: 7519 seeds (0 missing), 42 candidates, 0 admitted, "
        "skipped 42 (corroboration=12, no_tranche=30)" in row["detail"]
    )
    assert "refresh: 299/300 refreshed (12 dormant), 0 missing" in row["detail"]
    assert "events: 6 carded from 58 changes, 52 already carded" in row["detail"]
    assert "credits: 5 carded from 19 attachments, 14 already carded" in row["detail"]
    assert "release dates: 3 carded from 11 changes, 8 already carded" in row["detail"]


async def test_admin_run_list_respects_limit(admin_authed_client, session):
    await _seed_runs(session)
    r = await admin_authed_client.get("/admin/runs", params={"limit": 1})
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_admin_gets_run_detail(admin_authed_client, session):
    tmdb, _ = await _seed_runs(session)
    r = await admin_authed_client.get(f"/admin/runs/{tmdb.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(tmdb.id)
    assert body["kind"] == "tmdb"
    assert body["status"] == "succeeded"
    assert body["items_processed"] == 5
    assert body["items_failed"] == 1


async def test_admin_run_detail_unknown_returns_404(admin_authed_client):
    r = await admin_authed_client.get(f"/admin/runs/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_run_detail_includes_llm_usage(admin_authed_client, session):
    from decimal import Decimal

    from upmovies.ingest.models import RunLLMUsage

    tmdb, _ = await _seed_runs(session)
    link = IngestRun(kind="link", status="succeeded")
    session.add(link)
    await session.commit()
    await session.refresh(link)
    session.add_all(
        [
            RunLLMUsage(
                run_id=link.id,
                stage="link",
                model="claude-haiku-4-5",
                batched=True,
                input_tokens=100,
                output_tokens=10,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=50,
                cost_usd=Decimal("0.001234"),
            ),
            RunLLMUsage(
                run_id=link.id,
                stage="cluster",
                model="claude-sonnet-4-6",
                batched=True,
                input_tokens=200,
                output_tokens=20,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                cost_usd=Decimal("0.005000"),
            ),
        ]
    )
    await session.commit()

    r = await admin_authed_client.get(f"/admin/runs/{link.id}")
    assert r.status_code == 200
    body = r.json()
    usage = {u["stage"]: u for u in body["llm_usage"]}
    assert set(usage) == {"link", "cluster"}
    assert usage["link"]["model"] == "claude-haiku-4-5"
    assert usage["link"]["batched"] is True
    assert usage["link"]["input_tokens"] == 100
    assert usage["link"]["cache_read_input_tokens"] == 900
    assert usage["link"]["cost_usd"] == 0.001234
    assert usage["cluster"]["model"] == "claude-sonnet-4-6"


async def test_run_list_includes_llm_usage(admin_authed_client, session):
    from decimal import Decimal

    from upmovies.ingest.models import RunLLMUsage

    link = IngestRun(kind="link", status="succeeded")
    session.add(link)
    await session.commit()
    await session.refresh(link)
    session.add(
        RunLLMUsage(
            run_id=link.id,
            stage="link",
            model="claude-haiku-4-5",
            batched=False,
            input_tokens=5,
            cost_usd=Decimal("0.000005"),
        )
    )
    await session.commit()

    r = await admin_authed_client.get("/admin/runs")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert rows[str(link.id)]["llm_usage"][0]["stage"] == "link"


async def test_run_without_usage_serializes_empty_list(admin_authed_client, session):
    tmdb, _ = await _seed_runs(session)
    r = await admin_authed_client.get(f"/admin/runs/{tmdb.id}")
    assert r.status_code == 200
    assert r.json()["llm_usage"] == []


# --- retrieval health (NEU-997) -------------------------------------------------


async def _seed_retrieval_health(
    session,
    *,
    started_at: datetime | None = None,
    stories_retrieved: int = 200,
    zero_candidate_stories: int = 50,
    saturated_stories: int = 10,
    mean_candidates: float | None = 2.5,
    soft_breach: bool = False,
    picks: Sequence[bool] = (),
) -> IngestRun:
    """A `link` run with a health row, plus one probe row per entry in `picks` (True where
    retrieval surfaced the roster's film)."""
    run = IngestRun(kind="link", status="succeeded")
    if started_at is not None:
        run.started_at = started_at
    session.add(run)
    await session.flush()
    session.add(
        RunRetrievalHealth(
            run_id=run.id,
            stories_retrieved=stories_retrieved,
            zero_candidate_stories=zero_candidate_stories,
            saturated_stories=saturated_stories,
            mean_candidates=mean_candidates,
            soft_breach=soft_breach,
        )
    )
    for index, retrieved in enumerate(picks):
        film = Film(tmdb_id=next(_tmdb_ids), title=f"Runner {index}")
        story = Story(source="X", url=f"https://e/{uuid.uuid4()}", title="Runner news")
        session.add_all([film, story])
        await session.flush()
        session.add(
            LinkRetrievalProbe(
                run_id=run.id,
                story_id=story.id,
                film_id=film.id,
                retrieved=retrieved,
                rank=1 if retrieved else None,
                score=1.0 if retrieved else None,
                candidate_count=3,
            )
        )
    await session.commit()
    await session.refresh(run)
    return run


async def test_run_detail_includes_retrieval_health_rates(admin_authed_client, session):
    run = await _seed_retrieval_health(session, picks=(True, True, True, False))
    r = await admin_authed_client.get(f"/admin/runs/{run.id}")
    assert r.status_code == 200
    health = r.json()["retrieval_health"]
    assert health["stories_retrieved"] == 200
    assert health["zero_candidate_stories"] == 50
    assert health["saturated_stories"] == 10
    assert health["mean_candidates"] == 2.5
    assert health["zero_candidate_rate"] == 0.25
    assert health["saturation_rate"] == 0.05
    # Recall is measured against the roster's picks, so its denominator is the probe rows.
    assert health["roster_picks"] == 4
    assert health["roster_picks_retrieved"] == 3
    assert health["roster_pick_recall"] == 0.75
    assert health["soft_breach"] is False


async def test_run_list_includes_retrieval_health(admin_authed_client, session):
    run = await _seed_retrieval_health(session, picks=(True,))
    r = await admin_authed_client.get("/admin/runs")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert rows[str(run.id)]["retrieval_health"]["zero_candidate_rate"] == 0.25


async def test_run_without_a_health_row_serializes_null(admin_authed_client, session):
    # A missing row keeps its own meaning: shadow did not run at all.
    tmdb, _ = await _seed_runs(session)
    r = await admin_authed_client.get(f"/admin/runs/{tmdb.id}")
    assert r.status_code == 200
    assert r.json()["retrieval_health"] is None


async def test_health_without_probes_reports_no_recall(admin_authed_client, session):
    run = await _seed_retrieval_health(session, picks=())
    r = await admin_authed_client.get(f"/admin/runs/{run.id}")
    health = r.json()["retrieval_health"]
    assert health["roster_picks"] == 0
    assert health["roster_pick_recall"] is None


async def test_probe_counts_do_not_leak_between_runs(admin_authed_client, session):
    # The aggregate is grouped by run; a shared count would make every trend point identical.
    quiet = await _seed_retrieval_health(session, picks=(True,))
    busy = await _seed_retrieval_health(session, picks=(True, False))
    rows = {row["id"]: row for row in (await admin_authed_client.get("/admin/runs")).json()}
    assert rows[str(quiet.id)]["retrieval_health"]["roster_picks"] == 1
    assert rows[str(busy.id)]["retrieval_health"]["roster_picks"] == 2


# --- retrieval-health trend -----------------------------------------------------


async def test_trend_requires_authentication(anon_client):
    assert (await anon_client.get("/admin/runs/retrieval-health")).status_code == 401


async def test_trend_forbidden_for_non_admin(authed_client):
    assert (await authed_client.get("/admin/runs/retrieval-health")).status_code == 403


async def test_trend_returns_runs_newest_first(admin_authed_client, session):
    older = await _seed_retrieval_health(
        session, started_at=datetime(2026, 8, 1, tzinfo=UTC), zero_candidate_stories=20
    )
    newer = await _seed_retrieval_health(
        session, started_at=datetime(2026, 8, 5, tzinfo=UTC), zero_candidate_stories=80
    )
    r = await admin_authed_client.get("/admin/runs/retrieval-health")
    assert r.status_code == 200
    body = r.json()
    assert [point["run_id"] for point in body] == [str(newer.id), str(older.id)]
    # The drift the trend exists to show: the zero-candidate rate creeping up over runs.
    assert [point["zero_candidate_rate"] for point in body] == [0.4, 0.1]
    assert body[0]["run_status"] == "succeeded"


async def test_trend_omits_runs_that_never_ran_shadow(admin_authed_client, session):
    await _seed_runs(session)
    run = await _seed_retrieval_health(session)
    r = await admin_authed_client.get("/admin/runs/retrieval-health")
    assert [point["run_id"] for point in r.json()] == [str(run.id)]


async def test_trend_respects_limit(admin_authed_client, session):
    await _seed_retrieval_health(session, started_at=datetime(2026, 8, 1, tzinfo=UTC))
    newer = await _seed_retrieval_health(session, started_at=datetime(2026, 8, 5, tzinfo=UTC))
    r = await admin_authed_client.get("/admin/runs/retrieval-health", params={"limit": 1})
    assert [point["run_id"] for point in r.json()] == [str(newer.id)]


async def test_trend_carries_the_recall_against_roster_picks(admin_authed_client, session):
    await _seed_retrieval_health(session, picks=(True, True, False, False))
    r = await admin_authed_client.get("/admin/runs/retrieval-health")
    assert r.json()[0]["roster_pick_recall"] == 0.5


async def test_trend_order_is_stable_for_runs_sharing_a_start(admin_authed_client, session):
    # A series compared against itself cannot have `limit` drop a different point each call.
    same = datetime(2026, 8, 5, tzinfo=UTC)
    runs = [await _seed_retrieval_health(session, started_at=same) for _ in range(3)]
    expected = [str(run.id) for run in sorted(runs, key=lambda r: r.id, reverse=True)]
    for _ in range(3):
        r = await admin_authed_client.get("/admin/runs/retrieval-health")
        assert [point["run_id"] for point in r.json()] == expected


async def test_trend_since_bounds_the_window(admin_authed_client, session):
    await _seed_retrieval_health(session, started_at=datetime(2026, 7, 20, tzinfo=UTC))
    inside = await _seed_retrieval_health(session, started_at=datetime(2026, 8, 5, tzinfo=UTC))
    r = await admin_authed_client.get(
        "/admin/runs/retrieval-health", params={"since": "2026-08-01T00:00:00Z"}
    )
    assert [point["run_id"] for point in r.json()] == [str(inside.id)]


async def test_trend_since_without_an_offset_is_read_as_utc(admin_authed_client, session):
    run = await _seed_retrieval_health(session, started_at=datetime(2026, 8, 5, tzinfo=UTC))
    r = await admin_authed_client.get(
        "/admin/runs/retrieval-health", params={"since": "2026-08-01T00:00:00"}
    )
    assert [point["run_id"] for point in r.json()] == [str(run.id)]


async def test_run_detail_surfaces_the_soft_breach_flag(admin_authed_client, session):
    """The soft tier has to be visible where drift is read (NEU-1088 §3.6). The flag is the
    run's verdict against the threshold in force *at the time*, which `saturation_rate` beside
    it cannot reconstruct — the threshold is a setting, and it is expected to move."""
    run = await _seed_retrieval_health(session, saturated_stories=40, soft_breach=True)
    r = await admin_authed_client.get(f"/admin/runs/{run.id}")
    assert r.status_code == 200
    health = r.json()["retrieval_health"]
    assert health["soft_breach"] is True
    assert health["saturation_rate"] == 0.2
