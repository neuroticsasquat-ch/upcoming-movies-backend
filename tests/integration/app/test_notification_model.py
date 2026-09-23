"""`app.notification` (D-31): the constraints the decision pass relies on.

The pass that writes these rows is NEU-1379; what is asserted here is the table's own
behaviour, because that is what the pass will be built against."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.fixtures.catalog import add_film
from upmovies.app.models import Notification, User
from upmovies.news.models import Event


@pytest.fixture
async def user(session):
    u = User(email="recipient@example.com", password_hash="x", display_name="Recipient")
    session.add(u)
    await session.commit()
    return u


@pytest.fixture
async def event(session):
    film = await add_film(session, tmdb_id=550)
    e = Event(
        film_id=film.id,
        event_type="release_date",
        confidence="confirmed",
        provenance="catalog",
        occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    session.add(e)
    await session.commit()
    return e


def _notification(user: User, event: Event, **overrides) -> Notification:
    values = {
        "user_id": user.id,
        "event_id": event.id,
        "kind": "alert",
        "channel": "email",
        "status": "queued",
    }
    return Notification(**(values | overrides))


async def test_one_decision_per_user_event_kind_and_channel(session, user, event):
    """The key that makes the decision pass re-runnable: a pass whose window overlaps a
    previous one reconsiders events it has already decided, and the second decision must be a
    conflict rather than a second mail."""
    session.add(_notification(user, event))
    await session.commit()

    session.add(_notification(user, event))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_the_same_event_may_alert_and_digest_on_each_channel(session, user, event):
    """Four rows, not four duplicates: an alert and a digest line about one event, by mail and
    by push (D-36), are different deliveries of the same news."""
    for kind in ("alert", "digest"):
        for channel in ("email", "push"):
            session.add(_notification(user, event, kind=kind, channel=channel))
    await session.commit()

    rows = (await session.execute(select(Notification))).scalars().all()
    assert len(rows) == 4


@pytest.mark.parametrize(
    ("column", "value"),
    [("kind", "newsletter"), ("channel", "sms"), ("status", "pending")],
)
async def test_the_closed_vocabularies_are_enforced_in_the_database(
    session, user, event, column, value
):
    session.add(_notification(user, event, **{column: value}))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_deleting_the_user_takes_their_notifications_with_them(session, user, event):
    session.add(_notification(user, event))
    await session.commit()

    await session.delete(user)
    await session.commit()

    assert (await session.execute(select(Notification))).scalars().all() == []
