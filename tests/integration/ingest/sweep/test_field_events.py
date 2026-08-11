"""The sweep's field-change phase end to end: which `catalog.film_field_change` rows become
events, and what stops the same change becoming two.

The rules that carry the weight here are the ones a re-run exercises. The phase reads a fixed
rolling window rather than a watermark, so every change is offered to it several days running;
if the skip check were wrong the feed would fill with duplicate cards within a week. The other
is the ADR-0002 interaction: a story-triggered release-date event and a catalog-triggered one
read the same change row, and must never both card it.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import FilmFieldChange
from upmovies.ingest.models import IngestRun
from upmovies.ingest.sweep import field_events, run_field_change_events
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL, TEMPLATE_VERSION

NOW = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
LOOKBACK_DAYS = 7
WINDOW_DAYS = 14


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
        "corroboration_window_days": WINDOW_DAYS,
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


async def test_a_first_release_date_cards_a_summarized_catalog_event(
    session, session_factory, run_id
):
    film = await add_film(session, 1, release_date=date(2026, 12, 4), title="Runner")
    await _change(session, film, field="release_date", old=None, new="2026-12-04")
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created, result.skipped) == (1, 1, 0)
    (event,) = await _events(session, film)
    assert event.event_type == "release_date"
    assert event.provenance == "catalog"
    assert event.confidence == "confirmed"
    # The change's own timestamp, so a backlog worked off after an outage is not all dated today.
    assert event.occurred_at == YESTERDAY
    # Per-country dates have no change history to read, so there is never a region (ADR-0002).
    assert event.region is None
    summary = (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one()
    assert summary.summary == "Release date set to 4 December 2026."
    assert summary.model == DETERMINISTIC_MODEL
    assert summary.prompt_version == TEMPLATE_VERSION


async def test_a_moved_release_date_cards_both_dates(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=date(2026, 12, 4))
    await _change(session, film, field="release_date", old="2026-08-14", new="2026-12-04")
    await session.commit()

    await _run(session_factory, run_id)

    (event,) = await _events(session, film)
    summary = (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one()
    assert summary.summary == "Release date moved from 14 August 2026 to 4 December 2026."


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
    await _change(session, film, field="release_date", old="2026-12-04", new=None)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created, result.skipped) == (2, 0, 0)
    assert await _events(session, film) == []


async def test_a_second_pass_over_the_same_window_cards_nothing_new(
    session, session_factory, run_id
):
    """The window is a fixed rolling one, so every change is re-read for days. This is the
    property that keeps that free."""
    film = await add_film(session, 1, release_date=date(2026, 12, 4))
    await _change(session, film, field="release_date", old=None, new="2026-12-04")
    await _change(
        session,
        film,
        field="status",
        old="Planned",
        new="In Production",
        changed_at=NOW - timedelta(days=2),
    )
    await session.commit()

    first = await _run(session_factory, run_id)
    second = await _run(session_factory, run_id)

    assert first.events_created == 2
    assert (second.events_created, second.skipped) == (0, 2)
    assert len(await _events(session, film)) == 2


async def test_changes_older_than_the_lookback_are_never_read(session, session_factory, run_id):
    """`film_field_change` holds months of history. Without the floor, the first pass after
    deploy would card every date move ever recorded."""
    film = await add_film(session, 1, release_date=date(2026, 12, 4))
    await _change(
        session,
        film,
        field="release_date",
        old=None,
        new="2026-12-04",
        changed_at=NOW - timedelta(days=LOOKBACK_DAYS + 1),
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created) == (0, 0)
    assert await _events(session, film) == []


async def test_a_story_event_for_the_same_move_is_not_carded_twice(
    session, session_factory, run_id
):
    """ADR-0002's path ran first — `link` corroborated a held story against this very change
    and carded it, dating the event to the story rather than to the change. Carding it again
    here would put two release-date cards on the film for one move."""
    film = await add_film(session, 1, release_date=date(2026, 12, 4))
    await _change(session, film, field="release_date", old=None, new="2026-12-04")
    session.add(
        Event(
            film_id=film.id,
            event_type="release_date",
            confidence="confirmed",
            occurred_at=YESTERDAY - timedelta(days=2),
        )
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.events_created, result.skipped) == (0, 1)
    assert len(await _events(session, film)) == 1


async def test_a_release_date_event_outside_the_window_does_not_suppress_the_card(
    session, session_factory, run_id
):
    film = await add_film(session, 1, release_date=date(2026, 12, 4))
    await _change(session, film, field="release_date", old="2026-08-14", new="2026-12-04")
    session.add(
        Event(
            film_id=film.id,
            event_type="release_date",
            confidence="confirmed",
            occurred_at=YESTERDAY - timedelta(days=WINDOW_DAYS + 1),
        )
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    assert len(await _events(session, film)) == 2


async def test_each_film_commits_on_its_own(session, session_factory, run_id, monkeypatch):
    """Commit per item: one film whose write blows up must not cost the others."""
    first = await add_film(session, 1, release_date=date(2026, 12, 4))
    second = await add_film(session, 2, release_date=date(2026, 12, 5))
    await _change(session, first, field="release_date", old=None, new="2026-12-04")
    await _change(
        session,
        second,
        field="release_date",
        old=None,
        new="2026-12-05",
        changed_at=NOW - timedelta(hours=1),
    )
    await session.commit()

    real = field_events.write_deterministic_summary

    async def explode(session_, *, event_id, change, source_updated_at):
        if isinstance(change, field_events.ReleaseDateSet) and change.new_date == date(2026, 12, 4):
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
        film = await add_film(session, i, release_date=date(2026, 12, 4))
        await _change(
            session,
            film,
            field="release_date",
            old=None,
            new="2026-12-04",
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
    film = await add_film(session, 1, release_date=date(2026, 12, 4))
    await _change(session, film, field="release_date", old=None, new="2026-12-04")
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
