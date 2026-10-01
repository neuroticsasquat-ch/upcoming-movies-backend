"""Re-rendering deterministic now_available summaries after the store bodies dropped providers."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from scripts.rerender_now_available_summaries import rerender
from tests.fixtures.catalog import add_film
from upmovies.catalog.models import AvailabilityFirstSeen, WatchProvider
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL, TEMPLATE_VERSION

NOW = datetime(2026, 9, 20, tzinfo=UTC)


async def _event(session, film, *kinds, event_type="now_available"):
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="confirmed",
        provenance="catalog",
        occurred_at=NOW,
        region="US",
        subject_key=[f"US:{kind}" for kind in kinds],
    )
    session.add(event)
    await session.flush()
    return event


async def _summary(session, event, body, *, model=None, edited=None):
    session.add(
        EventSummary(
            event_id=event.id,
            summary=body,
            model=model or DETERMINISTIC_MODEL,
            prompt_version="deterministic-9",
            source_updated_at=NOW,
            edited_at=edited,
        )
    )
    await session.flush()


async def _first_seen(session, film, provider_id, kind, *, at=NOW):
    session.add(
        AvailabilityFirstSeen(
            film_id=film.id,
            region="US",
            provider_id=provider_id,
            monetization_type=kind,
            first_seen_at=at,
        )
    )
    await session.flush()


async def _providers(session):
    session.add_all(
        [
            WatchProvider(id=8, name="Netflix"),
            WatchProvider(id=15, name="Hulu"),
            WatchProvider(id=2, name="Apple TV"),
        ]
    )
    await session.flush()


async def _row(session, event) -> EventSummary:
    return (
        await session.execute(
            select(EventSummary).where(EventSummary.event_id == event.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def test_a_dry_run_rewrites_nothing(session):
    film = await add_film(session, 1)
    event = await _event(session, film, "rent")
    await _summary(session, event, "Available to rent on Apple TV.")
    await session.commit()

    assert await rerender(session, apply=False) == [
        ("Available to rent on Apple TV.", "Available to rent.")
    ]
    assert (await _row(session, event)).summary == "Available to rent on Apple TV."


async def test_rent_and_buy_collapse_into_one_sentence(session):
    film = await add_film(session, 1)
    event = await _event(session, film, "rent", "buy")
    await _summary(session, event, "Available to rent on Apple TV. Available to buy on Apple TV.")
    await session.commit()

    assert len(await rerender(session, apply=True)) == 1
    row = await _row(session, event)
    assert row.summary == "Available to rent or buy."
    assert row.prompt_version == TEMPLATE_VERSION


async def test_streaming_providers_are_read_back_from_the_ledger_in_observed_order(session):
    """The streaming clause still names its services, and the ledger is where they live: the
    rows this observation inserted share the event's `occurred_at`, and their ids follow the
    order the poll saw them in."""
    await _providers(session)
    film = await add_film(session, 1)
    await _first_seen(session, film, 15, "flatrate")
    await _first_seen(session, film, 8, "flatrate")
    await _first_seen(session, film, 2, "rent")
    # A later observation's streaming row is a different card's, not this one's.
    await _first_seen(session, film, 2, "flatrate", at=NOW + timedelta(days=3))
    event = await _event(session, film, "flatrate", "rent")
    await _summary(
        session, event, "Now streaming on Hulu and Netflix. Available to rent on Apple TV."
    )
    await session.commit()

    await rerender(session, apply=True)
    assert (await _row(session, event)).summary == (
        "Now streaming on Hulu and Netflix. Available to rent."
    )


async def test_a_streaming_card_with_no_ledger_rows_is_left_alone(session):
    film = await add_film(session, 1)
    event = await _event(session, film, "flatrate", "buy")
    await _summary(session, event, "Now streaming on Netflix. Available to buy on Apple TV.")
    await session.commit()

    assert await rerender(session, apply=True) == []
    assert (await _row(session, event)).summary == (
        "Now streaming on Netflix. Available to buy on Apple TV."
    )


async def test_a_hand_edited_summary_is_never_touched(session):
    film = await add_film(session, 1)
    event = await _event(session, film, "rent")
    await _summary(session, event, "Rentable everywhere.", edited=NOW)
    await session.commit()

    assert await rerender(session, apply=True) == []
    assert (await _row(session, event)).summary == "Rentable everywhere."


async def test_an_already_current_body_is_not_reported_as_work(session):
    film = await add_film(session, 1)
    event = await _event(session, film, "buy")
    await _summary(session, event, "Available to buy.")
    await session.commit()

    assert await rerender(session, apply=True) == []


async def test_other_event_types_are_out_of_scope(session):
    film = await add_film(session, 1)
    event = await _event(session, film, event_type="production_start")
    await _summary(session, event, "Shooting has started.")
    await session.commit()

    assert await rerender(session, apply=True) == []
