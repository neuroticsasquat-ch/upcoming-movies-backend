"""Re-rendering deterministic status summaries after a template reword."""

from datetime import UTC, datetime

from sqlalchemy import select

from scripts.rerender_status_summaries import rerender
from tests.fixtures.catalog import add_film
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL

NOW = datetime(2026, 8, 11, tzinfo=UTC)


async def _event(session, film, *, event_type="production_start", provenance="catalog"):
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="confirmed",
        provenance=provenance,
        occurred_at=NOW,
    )
    session.add(event)
    await session.flush()
    return event


async def _summary(
    session, event, *, body="The film has entered production.", model=None, edited=None
):
    session.add(
        EventSummary(
            event_id=event.id,
            summary=body,
            model=model or DETERMINISTIC_MODEL,
            prompt_version="deterministic-1",
            source_updated_at=NOW,
            edited_at=edited,
        )
    )
    await session.flush()


async def _body(session, event) -> str:
    return (
        await session.execute(
            select(EventSummary.summary).where(EventSummary.event_id == event.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def test_a_dry_run_rewrites_nothing(session):
    film = await add_film(session, 1)
    event = await _event(session, film)
    await _summary(session, event)
    await session.commit()

    assert len(await rerender(session, apply=False)) == 1
    assert await _body(session, event) == "The film has entered production."


async def test_apply_rewrites_the_stale_body(session):
    film = await add_film(session, 1)
    event = await _event(session, film)
    await _summary(session, event)
    await session.commit()

    assert len(await rerender(session, apply=True)) == 1
    assert await _body(session, event) == "Shooting has started."


async def test_a_wrap_event_reconstructs_its_own_status(session):
    film = await add_film(session, 1)
    event = await _event(session, film, event_type="production_wrap")
    await _summary(session, event, body="The film has entered post-production.")
    await session.commit()

    await rerender(session, apply=True)
    assert await _body(session, event) == "Shooting has wrapped."


async def test_a_hand_edited_summary_is_never_touched(session):
    film = await add_film(session, 1)
    event = await _event(session, film)
    await _summary(session, event, body="Cameras rolled in Malta.", edited=NOW)
    await session.commit()

    assert await rerender(session, apply=True) == []
    assert await _body(session, event) == "Cameras rolled in Malta."


async def test_an_llm_written_summary_is_never_touched(session):
    film = await add_film(session, 1)
    event = await _event(session, film)
    await _summary(session, event, body="A model wrote this.", model="claude-haiku-4-5")
    await session.commit()

    assert await rerender(session, apply=True) == []
    assert await _body(session, event) == "A model wrote this."


async def test_an_already_current_body_is_not_reported_as_work(session):
    film = await add_film(session, 1)
    event = await _event(session, film)
    await _summary(session, event, body="Shooting has started.")
    await session.commit()

    assert await rerender(session, apply=True) == []


async def test_release_date_events_are_out_of_scope(session):
    film = await add_film(session, 1)
    event = await _event(session, film, event_type="release_date")
    await _summary(session, event, body="US wide release date set to 4 December 2026.")
    await session.commit()

    assert await rerender(session, apply=True) == []
