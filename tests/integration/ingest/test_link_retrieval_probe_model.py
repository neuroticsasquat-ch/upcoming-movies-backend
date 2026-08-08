"""`ingest.link_retrieval_probe` + `ingest.run_retrieval_health` — shadow-mode telemetry
for candidate retrieval (NEU-994).

Two grains, because one cannot express both questions. The probe is **one row per story the
roster linked**: it answers "would retrieval have offered the film the roster picked, and
where in the list?" — the per-story disagreements a human adjudicates before cutover. The
health row is **one row per run**: it carries the denominator and the retrieval-health
signals, none of which are recoverable from the probe rows, because the stories that produce
those signals are precisely the ones the probe does not record.

These tests pin the constraints, not the writers — nothing writes either table yet (that is
NEU-996's shadow wiring).
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun, LinkRetrievalProbe, RunRetrievalHealth
from upmovies.ingest.runs import create_run
from upmovies.news.models import Story


async def _film(session, tmdb_id: int = 1, title: str = "Runner") -> Film:
    film = Film(tmdb_id=tmdb_id, title=title)
    session.add(film)
    await session.flush()
    return film


async def _story(session, url: str = "https://e/1") -> Story:
    story = Story(source="X", url=url, title="Runner news", link_status="linked")
    session.add(story)
    await session.flush()
    return story


def _probe(run_id: UUID, story_id: UUID, film_id: UUID, **overrides) -> LinkRetrievalProbe:
    fields: dict = {
        "run_id": run_id,
        "story_id": story_id,
        "film_id": film_id,
        "retrieved": True,
        "rank": 1,
        "score": 1.0,
        "candidate_count": 3,
    }
    fields.update(overrides)
    return LinkRetrievalProbe(**fields)


async def _fixture(session, *, url: str = "https://e/1", tmdb_id: int = 1):
    """A run, a linked story and the film the roster picked for it."""
    run_id = await create_run(session, kind="link")
    film = await _film(session, tmdb_id=tmdb_id)
    story = await _story(session, url=url)
    await session.commit()
    return run_id, story.id, film.id


# --------------------------------------------------------------------------- probe rows


async def test_insert_and_read_back_a_probe_row(session):
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id, rank=2, score=0.75, candidate_count=4))
    await session.commit()

    got = (
        await session.execute(select(LinkRetrievalProbe).where(LinkRetrievalProbe.run_id == run_id))
    ).scalar_one()
    assert got.story_id == story_id
    assert got.film_id == film_id
    assert got.retrieved is True
    assert got.rank == 2
    assert got.score == 0.75
    assert got.candidate_count == 4
    assert got.created_at is not None


async def test_a_lexical_miss_records_neither_rank_nor_score(session):
    """Retrieval never scored the roster's pick at all — the failure ADR-0008 calls a
    score-zero miss, which no amount of ranking or cap-raising would recover."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(
        _probe(run_id, story_id, film_id, retrieved=False, rank=None, score=None, candidate_count=2)
    )
    await session.commit()

    got = (
        await session.execute(select(LinkRetrievalProbe).where(LinkRetrievalProbe.run_id == run_id))
    ).scalar_one()
    assert got.retrieved is False
    assert got.rank is None
    assert got.score is None


async def test_a_cap_loss_records_a_score_with_no_rank(session):
    """A film that cleared the threshold but fell outside the cap has a score and no rank.

    Storing both is what separates the two failures the shadow period has to tell apart: a
    cap loss is fixed by raising K, a lexical miss is not fixable that way at all."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(
        _probe(run_id, story_id, film_id, retrieved=False, rank=None, score=0.5, candidate_count=10)
    )
    await session.commit()

    got = (
        await session.execute(select(LinkRetrievalProbe).where(LinkRetrievalProbe.run_id == run_id))
    ).scalar_one()
    assert got.retrieved is False
    assert got.rank is None
    assert got.score == 0.5


async def test_a_story_with_no_candidates_at_all_is_recordable(session):
    """The roster linked it, retrieval offered nothing — the disagreement that matters most,
    and the one a `candidate_count` of 0 has to be able to express."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(
        _probe(run_id, story_id, film_id, retrieved=False, rank=None, score=None, candidate_count=0)
    )
    await session.commit()

    got = (
        await session.execute(select(LinkRetrievalProbe).where(LinkRetrievalProbe.run_id == run_id))
    ).scalar_one()
    assert got.candidate_count == 0


@pytest.mark.parametrize(
    ("retrieved", "rank"),
    [(True, None), (False, 1)],
    ids=["retrieved-without-a-rank", "rank-without-retrieved"],
)
async def test_retrieved_cannot_disagree_with_rank(session, retrieved, rank):
    """`retrieved` restates "has a rank" and the constraint keeps it from ever saying
    otherwise — a redundant column that can lie is worse than no column."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id, retrieved=retrieved, rank=rank))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_a_ranked_pick_must_carry_a_score(session):
    """Nothing is offered without clearing the threshold, so a rank with no score is the one
    pairing the retriever cannot produce — and the only one the other checks left open."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id, rank=1, score=None))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_rank_is_one_based(session):
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id, rank=0))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_rank_cannot_exceed_the_candidate_count(session):
    """Rank is a position in the set the model was shown, so it is bounded by that set."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id, rank=4, candidate_count=3))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.parametrize("score", [-0.1, 1.5])
async def test_score_stays_inside_the_zero_to_one_fraction(session, score):
    """The scorer returns a fraction of a title's tokens; anything outside [0, 1] is a
    caller bug, not data."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id, score=score))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_candidate_count_cannot_be_negative(session):
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id, candidate_count=-1))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_one_probe_row_per_run_and_story(session):
    """Unlike `llm_call`, whose many-rows-per-stage is the point, the probe's grain *is* one
    row per linked story — so a second write for the same pair is a bug, not more data."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id))
    await session.commit()

    session.add(_probe(run_id, story_id, film_id, rank=2))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_the_same_story_can_be_probed_by_two_runs(session):
    """A story re-linked on a later run gets its own row — the uniqueness is per (run, story),
    which is what makes recall readable across the seven consecutive runs the gate needs."""
    run_id, story_id, film_id = await _fixture(session)
    other_run_id = await create_run(session, kind="link")
    await session.commit()

    session.add_all(
        [
            _probe(run_id, story_id, film_id),
            _probe(other_run_id, story_id, film_id),
        ]
    )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(LinkRetrievalProbe).where(LinkRetrievalProbe.story_id == story_id)
            )
        )
        .scalars()
        .all()
    )
    assert {r.run_id for r in rows} == {run_id, other_run_id}


async def test_deleting_the_run_cascades_to_its_probe_rows(session):
    """Core-level delete, so this exercises the FK's ON DELETE CASCADE rather than
    SQLAlchemy's in-Python delete-orphan cascade."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id))
    await session.commit()

    await session.execute(delete(IngestRun).where(IngestRun.id == run_id))
    await session.commit()

    remaining = (
        (
            await session.execute(
                select(LinkRetrievalProbe).where(LinkRetrievalProbe.run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


async def test_deleting_the_story_cascades_to_its_probe_rows(session):
    """These rows are diagnostics *about* a story; once it is gone there is nothing left to
    adjudicate, and a dangling probe row would only distort the recall denominator."""
    run_id, story_id, film_id = await _fixture(session)
    session.add(_probe(run_id, story_id, film_id))
    await session.commit()

    await session.execute(delete(Story).where(Story.id == story_id))
    await session.commit()

    remaining = (
        (
            await session.execute(
                select(LinkRetrievalProbe).where(LinkRetrievalProbe.run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


# --------------------------------------------------------------- per-run health aggregates


async def test_insert_and_read_back_a_health_row(session):
    run_id = await create_run(session, kind="link")
    await session.commit()
    session.add(
        RunRetrievalHealth(
            run_id=run_id,
            stories_retrieved=120,
            zero_candidate_stories=44,
            saturated_stories=3,
            mean_candidates=2.5,
        )
    )
    await session.commit()

    got = (
        await session.execute(select(RunRetrievalHealth).where(RunRetrievalHealth.run_id == run_id))
    ).scalar_one()
    assert got.stories_retrieved == 120
    assert got.zero_candidate_stories == 44
    assert got.saturated_stories == 3
    assert got.mean_candidates == 2.5


async def test_health_counts_default_to_zero_with_no_mean(session):
    """A run that retrieved nothing has no average to report — NULL rather than a 0.0 that
    would read as "every story got zero candidates"."""
    run_id = await create_run(session, kind="link")
    await session.commit()
    session.add(RunRetrievalHealth(run_id=run_id))
    await session.commit()

    got = (
        await session.execute(select(RunRetrievalHealth).where(RunRetrievalHealth.run_id == run_id))
    ).scalar_one()
    assert got.stories_retrieved == 0
    assert got.zero_candidate_stories == 0
    assert got.saturated_stories == 0
    assert got.mean_candidates is None


@pytest.mark.parametrize(
    ("stories_retrieved", "mean_candidates"),
    [(0, 1.0), (10, None)],
    ids=["a-mean-with-no-denominator", "a-denominator-with-no-mean"],
)
async def test_the_mean_is_present_exactly_when_there_is_a_denominator(
    session, stories_retrieved, mean_candidates
):
    run_id = await create_run(session, kind="link")
    await session.commit()
    session.add(
        RunRetrievalHealth(
            run_id=run_id,
            stories_retrieved=stories_retrieved,
            mean_candidates=mean_candidates,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.parametrize("column", ["zero_candidate_stories", "saturated_stories"])
async def test_health_signals_cannot_exceed_the_denominator(session, column):
    """Both signals count stories drawn from `stories_retrieved`; exceeding it means the
    writer double-counted, and a rate above 1 would silently corrupt the M4 breach guard."""
    run_id = await create_run(session, kind="link")
    await session.commit()
    session.add(
        RunRetrievalHealth(run_id=run_id, stories_retrieved=5, mean_candidates=1.0, **{column: 6})
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_one_health_row_per_run(session):
    """The aggregate grain is the run, so a second row is a double-finalize bug."""
    run_id = await create_run(session, kind="link")
    await session.commit()
    session.add(RunRetrievalHealth(run_id=run_id))
    await session.commit()

    session.add(RunRetrievalHealth(run_id=run_id))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_deleting_the_run_cascades_to_its_health_row(session):
    run_id = await create_run(session, kind="link")
    await session.commit()
    session.add(RunRetrievalHealth(run_id=run_id))
    await session.commit()

    await session.execute(delete(IngestRun).where(IngestRun.id == run_id))
    await session.commit()

    remaining = (
        (
            await session.execute(
                select(RunRetrievalHealth).where(RunRetrievalHealth.run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


async def test_a_health_row_needs_a_real_run(session):
    """The FK is the guard that keeps aggregates attributable to a run that happened."""
    session.add(RunRetrievalHealth(run_id=uuid4()))
    with pytest.raises(IntegrityError):
        await session.commit()
