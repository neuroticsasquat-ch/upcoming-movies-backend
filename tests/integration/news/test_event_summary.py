from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from upmovies.app.models import User
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


async def test_event_summary_edited_columns_round_trip(session):
    """edited_at/edited_by persist and default to NULL (machine-generated) when unset."""
    film, event = await _film_and_event(session)
    user = User(email="editor@example.com", password_hash="x", display_name="Editor")
    session.add(user)
    await session.flush()

    edited_at = datetime.now(UTC)
    summary_obj = EventSummary(
        event_id=event.id,
        summary="Edited summary.",
        model="gpt-4",
        prompt_version="v1.0",
        source_updated_at=datetime.now(UTC),
        edited_at=edited_at,
        edited_by=user.id,
    )
    session.add(summary_obj)
    await session.commit()

    result = await session.get(
        EventSummary, event.id, execution_options={"populate_existing": True}
    )
    assert result is not None
    assert result.edited_at == edited_at
    assert result.edited_by == user.id


async def test_event_summary_edited_columns_default_null(session):
    """A summary written without the edit marker has NULL edited_at/edited_by."""
    film, event = await _film_and_event(session)
    summary_obj = EventSummary(
        event_id=event.id,
        summary="Machine summary.",
        model="gpt-4",
        prompt_version="v1.0",
        source_updated_at=datetime.now(UTC),
    )
    session.add(summary_obj)
    await session.commit()

    result = await session.get(
        EventSummary, event.id, execution_options={"populate_existing": True}
    )
    assert result is not None
    assert result.edited_at is None
    assert result.edited_by is None


async def test_event_summary_edited_by_set_null_on_user_delete(session):
    """Deleting the editing user nulls edited_by (ON DELETE SET NULL), keeping the summary."""
    film, event = await _film_and_event(session)
    user = User(email="gone@example.com", password_hash="x", display_name="Gone")
    session.add(user)
    await session.flush()

    summary_obj = EventSummary(
        event_id=event.id,
        summary="Edited summary.",
        model="gpt-4",
        prompt_version="v1.0",
        source_updated_at=datetime.now(UTC),
        edited_at=datetime.now(UTC),
        edited_by=user.id,
    )
    session.add(summary_obj)
    await session.commit()

    await session.delete(user)
    await session.commit()

    # The FK SET NULL is a DB-level action the ORM doesn't observe; expunge to force a re-read.
    session.expunge_all()
    result = await session.get(EventSummary, event.id)
    assert result is not None
    assert result.edited_by is None
    assert result.edited_at is not None  # the edit marker timestamp survives


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
