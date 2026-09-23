"""The sweep's field-change phase end to end: which `catalog.film_field_change` rows become
events, and what stops the same change becoming two.

The rules that carry the weight here are the ones a re-run exercises. The phase reads a fixed
rolling window rather than a watermark, so every change is offered to it several days running;
if the skip check were wrong the feed would fill with duplicate cards within a week.

Release dates left this phase in NEU-1121 — see `test_release_events.py`, which carries the
ADR-0002 anti-double-card rule that moved with them.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_credit, add_film
from upmovies.catalog.models import (
    Collection,
    Film,
    FilmFieldChange,
    FilmProductionCompany,
    ProductionCompany,
)
from upmovies.catalog.queries import (
    ALERT_WINDOW_DEAD_STATUSES,
    alert_window_clause,
    in_play_clause,
)
from upmovies.config import get_settings
from upmovies.ingest.models import IngestRun
from upmovies.ingest.sweep import (
    field_events,
    run_collection_events,
    run_company_events,
    run_field_change_events,
)
from upmovies.ingest.sweep.credit_events import run_credit_detachment_events
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL, TEMPLATE_VERSION

NOW = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
TODAY = NOW.date()
YESTERDAY = NOW - timedelta(days=1)
LOOKBACK_DAYS = 7


async def _change(session, film, *, field, old, new, changed_at=YESTERDAY):
    session.add(
        FilmFieldChange(
            film_id=film.id, field=field, old_value=old, new_value=new, changed_at=changed_at
        )
    )
    await session.flush()


async def _run(session_factory, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "run_id": run_id,
        "now": NOW,
        "lookback_days": LOOKBACK_DAYS,
    }
    return await run_field_change_events(**{**kwargs, **overrides})


async def _events(session, film):
    return (
        (
            await session.execute(
                select(Event).where(Event.film_id == film.id).order_by(Event.occurred_at),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


async def test_a_status_change_cards_a_summarized_catalog_event(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, title="Runner")
    await _change(session, film, field="status", old="Planned", new="In Production")
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created, result.skipped) == (1, 1, 0)
    (event,) = await _events(session, film)
    assert event.event_type == "production_start"
    assert event.provenance == "catalog"
    assert event.confidence == "confirmed"
    # The change's own timestamp, so a backlog worked off after an outage is not all dated today.
    assert event.occurred_at == YESTERDAY
    # Status is not a regional fact, so it is never region-tagged (NEU-446).
    assert event.region is None
    summary = (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one()
    assert summary.summary == "Shooting has started."
    assert summary.model == DETERMINISTIC_MODEL
    assert summary.prompt_version == TEMPLATE_VERSION


async def test_a_primary_release_date_change_is_read_and_ignored(session, session_factory, run_id):
    """NEU-1121: `film.release_date` is TMDB's earliest release in any country of any type,
    which is not what the page lists — so this phase no longer cards it at all. Displayable
    dates come from `film_release_date_change` via `sweep.release_events`."""
    film = await add_film(session, 1, release_date=date(2026, 12, 4))
    await _change(session, film, field="release_date", old=None, new="2026-12-04")
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created) == (0, 0)
    assert await _events(session, film) == []


async def test_status_transitions_card_the_production_milestones(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Post Production")
    await _change(
        session,
        film,
        field="status",
        old="Planned",
        new="In Production",
        changed_at=NOW - timedelta(days=3),
    )
    await _change(session, film, field="status", old="In Production", new="Post Production")
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 2
    assert [e.event_type for e in await _events(session, film)] == [
        "production_start",
        "production_wrap",
    ]


async def test_changes_that_are_not_beats_are_read_and_ignored(session, session_factory, run_id):
    film = await add_film(session, 1)
    await _change(session, film, field="status", old="Post Production", new="Released")
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created, result.skipped) == (1, 0, 0)
    assert await _events(session, film) == []


# --- cancellation (EF-6, NEU-1435) ---------------------------------------------


async def test_a_cancellation_cards_a_confirmed_summarized_event(session, session_factory, run_id):
    """The production milestones' shape exactly: catalog-sourced, `confirmed` because ADR-0002
    makes TMDB the record for its own scalar fields, dated when TMDB moved, and carrying the
    deterministic body — no quarantine anywhere in it, because a status is not a claim
    somebody made."""
    film = await add_film(session, 1, release_date=None, status="Canceled", title="Runner")
    await _change(session, film, field="status", old="Post Production", new="Canceled")
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created, result.skipped) == (1, 1, 0)
    (event,) = await _events(session, film)
    assert event.event_type == "canceled"
    assert event.provenance == "catalog"
    assert event.confidence == "confirmed"
    assert event.occurred_at == YESTERDAY
    assert event.region is None
    summary = (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one()
    assert summary.summary == "The film has been canceled."
    assert summary.model == DETERMINISTIC_MODEL


async def test_a_film_is_carded_as_cancelled_once(session, session_factory, run_id):
    """`ONCE_PER_FILM_EVENT_TYPES`: the rolling window re-offers the same change for days, and
    the card is what a later pass matches on."""
    film = await add_film(session, 1, release_date=None, status="Canceled")
    await _change(session, film, field="status", old="Planned", new="Canceled")
    await session.commit()

    first = await _run(session_factory, run_id)
    second = await _run(session_factory, run_id)

    assert first.events_created == 1
    assert (second.events_created, second.skipped) == (0, 1)
    assert [e.event_type for e in await _events(session, film)] == ["canceled"]


async def test_an_uncancellation_and_a_re_cancellation_do_not_card_again(
    session, session_factory, run_id
):
    """TMDB correcting itself is not two more beats. The `Canceled -> Planned` row classifies
    as nothing, and the `Planned -> Canceled` row that follows finds the first card — so a
    film that flaps carries exactly one cancellation card, and no "un-cancelled" card ever
    exists to contradict it."""
    film = await add_film(session, 1, release_date=None, status="Canceled")
    await _change(
        session,
        film,
        field="status",
        old="Post Production",
        new="Canceled",
        changed_at=NOW - timedelta(days=3),
    )
    await _change(
        session,
        film,
        field="status",
        old="Canceled",
        new="Planned",
        changed_at=NOW - timedelta(days=2),
    )
    await _change(session, film, field="status", old="Planned", new="Canceled")
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created, result.skipped) == (3, 1, 1)
    assert [e.event_type for e in await _events(session, film)] == ["canceled"]


async def test_a_cancellation_raises_no_detachment_cards(session, session_factory, run_id):
    """EF-6: "it fires **no detach cards**" — TMDB rarely strips a dead title's credits, and a
    run of "no longer attached" cards for a film that has already been called off would
    mislead. Pinned from the outside across all three attachment kinds the spec names (any
    credit, any company, the collection): every carding phase runs over a film that holds all
    three and whose status is the only thing that moved, and the only card that exists
    afterwards is the cancellation."""
    session.add(Collection(id=900, name="Runner Collection"))
    session.add(ProductionCompany(id=500, name="Legendary Pictures"))
    await session.flush()
    # The attachments are held from insert, not assigned afterwards: `film_field_change_trg` is
    # a BEFORE UPDATE trigger, so setting `collection_id` on a stored row would write a change
    # of its own and card a franchise arrival this test never meant to stage.
    film = await add_film(session, 1, release_date=None, status="Canceled", collection_id=900)
    await add_credit(session, film, 11, credit_type="crew", job="Director")
    await add_credit(session, film, 12, credit_type="cast", credit_order=0)
    session.add(FilmProductionCompany(film_id=film.id, company_id=500))
    await _change(session, film, field="status", old="In Production", new="Canceled")
    await session.commit()

    await _run(session_factory, run_id)
    detachments = await run_credit_detachment_events(
        session_factory=session_factory, run_id=run_id, now=NOW, lookback_days=LOOKBACK_DAYS
    )
    await run_company_events(
        session_factory=session_factory, run_id=run_id, now=NOW, lookback_days=LOOKBACK_DAYS
    )
    await run_collection_events(
        session_factory=session_factory, run_id=run_id, now=NOW, lookback_days=LOOKBACK_DAYS
    )

    assert detachments.detachments_read == 0
    assert [e.event_type for e in await _events(session, film)] == ["canceled"]


async def test_nothing_further_cards_for_a_cancelled_film(session, session_factory, run_id):
    """`Canceled` is terminal (EF-6). `in_play_clause` and `ALERT_WINDOW_DEAD_STATUSES` are
    where that is enforced — both spelled here rather than trusted, since this is the status
    they exist for and neither is exercised by this phase.

    `in_play_clause` is handed the *deployed* excluded set rather than a literal: its status
    term is `TMDB_EXCLUDED_STATUSES`, which is a setting and so the thing that could actually
    stop excluding `Canceled` one day. Passing the exclusion in and then asserting it would
    test SQL's `NOT IN`."""
    film = await add_film(session, 1, release_date=None, status="Canceled")
    await session.commit()
    excluded = get_settings().tmdb_excluded_statuses

    in_play = (
        await session.execute(
            select(Film.id).where(
                Film.id == film.id,
                in_play_clause(today=TODAY, excluded_statuses=excluded),
            )
        )
    ).scalar_one_or_none()
    covered = (
        await session.execute(
            select(Film.id).where(
                Film.id == film.id, alert_window_clause(today=TODAY, max_age_days=365)
            )
        )
    ).scalar_one_or_none()

    assert "Canceled" in excluded
    assert in_play is None
    assert covered is None
    assert "Canceled" in ALERT_WINDOW_DEAD_STATUSES


async def test_a_second_pass_over_the_same_window_cards_nothing_new(
    session, session_factory, run_id
):
    """The window is a fixed rolling one, so every change is re-read for days. This is the
    property that keeps that free."""
    film = await add_film(session, 1, release_date=None)
    await _change(
        session,
        film,
        field="status",
        old="Planned",
        new="In Production",
        changed_at=NOW - timedelta(days=2),
    )
    await _change(session, film, field="status", old="In Production", new="Post Production")
    await session.commit()

    first = await _run(session_factory, run_id)
    second = await _run(session_factory, run_id)

    assert first.events_created == 2
    assert (second.events_created, second.skipped) == (0, 2)
    assert len(await _events(session, film)) == 2


async def test_changes_older_than_the_lookback_are_never_read(session, session_factory, run_id):
    """`film_field_change` holds months of history. Without the floor, the first pass after
    deploy would card every status transition ever recorded."""
    film = await add_film(session, 1, release_date=None)
    await _change(
        session,
        film,
        field="status",
        old="Planned",
        new="In Production",
        changed_at=NOW - timedelta(days=LOOKBACK_DAYS + 1),
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created) == (0, 0)
    assert await _events(session, film) == []


async def test_each_film_commits_on_its_own(session, session_factory, run_id, monkeypatch):
    """Commit per item: one film whose write blows up must not cost the others."""
    first = await add_film(session, 1, release_date=None)
    second = await add_film(session, 2, release_date=None)
    await _change(session, first, field="status", old="Planned", new="In Production")
    await _change(
        session,
        second,
        field="status",
        old="In Production",
        new="Post Production",
        changed_at=NOW - timedelta(hours=1),
    )
    await session.commit()

    real = field_events.write_deterministic_summary

    async def explode(session_, *, event_id, change, source_updated_at):
        if change == field_events.StatusChanged(new_status="In Production"):
            raise RuntimeError("summary write failed")
        return await real(
            session_, event_id=event_id, change=change, source_updated_at=source_updated_at
        )

    monkeypatch.setattr(field_events, "write_deterministic_summary", explode)

    result = await _run(session_factory, run_id)

    assert (result.events_created, result.failures) == (1, 1)
    assert await _events(session, first) == []
    assert len(await _events(session, second)) == 1


async def test_consecutive_failures_abort_the_phase(session, session_factory, run_id, monkeypatch):
    for i in range(1, 4):
        film = await add_film(session, i, release_date=None)
        await _change(
            session,
            film,
            field="status",
            old="Planned",
            new="In Production",
            changed_at=NOW - timedelta(hours=i),
        )
    await session.commit()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(field_events, "write_deterministic_summary", explode)

    result = await _run(session_factory, run_id, failure_threshold=2)

    assert result.aborted is True
    assert result.abort_error == "aborted after 2 consecutive failures"
    assert result.failures == 2


async def test_the_run_counts_what_it_carded(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None)
    await _change(session, film, field="status", old="Planned", new="In Production")
    await session.commit()

    await _run(session_factory, run_id)

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.items_processed == 1


@pytest.mark.parametrize("field", ["title", "runtime"])
async def test_untracked_fields_are_never_even_loaded(session, session_factory, run_id, field):
    film = await add_film(session, 1)
    await _change(session, film, field=field, old="old", new="new")
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.changes_read == 0
