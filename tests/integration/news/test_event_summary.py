from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from upmovies.catalog.models import Film
from upmovies.news.models import Event, EventSummary


async def _film_and_event(session):
    """Helper to create a Film and Event for testing."""
    film = Film(tmdb_id=1, title="Test Film")
    session.add(film)
    await session.flush()
    event = Event(
        film_id=film.id,
        event_type="release_date",
        confidence="confirmed",
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return film, event


async def test_event_summary_insert_round_trip(session):
    """Test that EventSummary columns round-trip correctly through the database."""
    film, event = await _film_and_event(session)

    # Create and insert EventSummary
    source_updated_at = datetime.now(UTC)
    summary_obj = EventSummary(
        event_id=event.id,
        summary="This is a test summary of the event.",
        model="gpt-4",
        prompt_version="v1.0",
        source_updated_at=source_updated_at,
    )
    session.add(summary_obj)
    await session.commit()

    # Verify generated_at was set by server default
    assert summary_obj.generated_at is not None

    # Re-query to verify all columns persist correctly
    result = await session.get(
        EventSummary,
        summary_obj.event_id,
        execution_options={"populate_existing": True},
    )
    assert result is not None
    assert result.event_id == event.id
    assert result.summary == "This is a test summary of the event."
    assert result.model == "gpt-4"
    assert result.prompt_version == "v1.0"
    assert result.source_updated_at == source_updated_at
    assert result.generated_at is not None


async def test_event_summary_cascade_delete(session):
    """Test that deleting an Event cascades and deletes its EventSummary."""
    film, event = await _film_and_event(session)

    # Create and insert EventSummary
    summary_obj = EventSummary(
        event_id=event.id,
        summary="Test summary",
        model="gpt-4",
        prompt_version="v1.0",
        source_updated_at=datetime.now(UTC),
    )
    session.add(summary_obj)
    await session.commit()

    # Verify EventSummary exists
    result = await session.get(EventSummary, event.id)
    assert result is not None

    # Save event_id before deletion
    event_id = event.id
    # Delete the Event
    await session.delete(event)
    await session.commit()

    # Verify the cascade delete removed both the Event and EventSummary
    session.expunge_all()
    assert await session.get(Event, event_id) is None
    assert await session.get(EventSummary, event_id) is None


async def test_event_summary_pk_uniqueness(session):
    """Test that inserting a second EventSummary for the same event_id raises IntegrityError."""
    film, event = await _film_and_event(session)

    # Create and insert first EventSummary
    summary1 = EventSummary(
        event_id=event.id,
        summary="First summary",
        model="gpt-4",
        prompt_version="v1.0",
        source_updated_at=datetime.now(UTC),
    )
    session.add(summary1)
    await session.commit()

    # Try to insert second EventSummary for the same event_id
    summary2 = EventSummary(
        event_id=event.id,
        summary="Second summary",
        model="gpt-4",
        prompt_version="v1.0",
        source_updated_at=datetime.now(UTC),
    )
    session.add(summary2)
    with pytest.raises(IntegrityError):
        await session.commit()
