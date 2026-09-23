"""The one-off backfill that marks attachment cards superseded by removal cards that were
written before the supersession write shipped (NEU-1347).

Forward-only and idempotent: a second run over the same ledger changes nothing, and a removal
card that already supersedes something is skipped rather than re-applied.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from scripts.backfill_credit_supersessions import backfill
from tests.fixtures.catalog import add_film
from upmovies.news.catalog_events import CREDIT_REMOVED_EVENT_TYPE
from upmovies.news.models import Event

REMOVED_AT = datetime(2026, 8, 1, tzinfo=UTC)
ATTACHED_AT = REMOVED_AT - timedelta(days=10)


async def _card(session, film, *, event_type, occurred_at, names, provenance="catalog"):
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="rumored",
        provenance=provenance,
        occurred_at=occurred_at,
        region=None,
        subject_key=names,
    )
    session.add(event)
    await session.flush()
    return event


async def _reload(session, event_id):
    return (
        await session.execute(
            select(Event).where(Event.id == event_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def test_backfill_marks_the_prior_attachment_for_each_existing_removal_card(
    session, session_factory
):
    film = await add_film(session, 1, release_date=None, status="Planned")
    attachment = await _card(
        session,
        film,
        event_type="crew_attached",
        occurred_at=ATTACHED_AT,
        names=["denis villeneuve"],
    )
    removal = await _card(
        session,
        film,
        event_type=CREDIT_REMOVED_EVENT_TYPE,
        occurred_at=REMOVED_AT,
        names=["denis villeneuve"],
    )
    await session.commit()

    result = await backfill(session_factory)

    assert (result.removals_read, result.superseded, result.skipped) == (1, 1, 0)
    attachment = await _reload(session, attachment.id)
    assert (attachment.status, attachment.superseded_by) == ("superseded", removal.id)
    removal = await _reload(session, removal.id)
    assert (removal.status, removal.superseded_by) == ("published", None)


async def test_backfill_is_idempotent(session, session_factory):
    """A second run skips every removal card the first one processed and rewrites nothing."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    older = await _card(
        session,
        film,
        event_type="casting",
        occurred_at=ATTACHED_AT - timedelta(days=30),
        names=["greta gerwig"],
        provenance="story",
    )
    await _card(
        session, film, event_type="crew_attached", occurred_at=ATTACHED_AT, names=["greta gerwig"]
    )
    await _card(
        session,
        film,
        event_type=CREDIT_REMOVED_EVENT_TYPE,
        occurred_at=REMOVED_AT,
        names=["greta gerwig"],
    )
    await session.commit()

    first = await backfill(session_factory)
    second = await backfill(session_factory)

    assert (first.superseded, first.skipped) == (1, 0)
    assert (second.superseded, second.skipped) == (0, 1)
    # The older casting card is *not* reached on the second pass: the removal is done.
    older = await _reload(session, older.id)
    assert (older.status, older.superseded_by) == ("published", None)


async def test_backfill_handles_attach_remove_reattach_remove(session, session_factory):
    """Each removal card marks the attachment card that was current before it."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    first_join = await _card(
        session, film, event_type="casting", occurred_at=ATTACHED_AT, names=["zendaya"]
    )
    first_leave = await _card(
        session,
        film,
        event_type=CREDIT_REMOVED_EVENT_TYPE,
        occurred_at=REMOVED_AT,
        names=["zendaya"],
    )
    rejoin = await _card(
        session,
        film,
        event_type="casting",
        occurred_at=REMOVED_AT + timedelta(days=5),
        names=["zendaya"],
    )
    second_leave = await _card(
        session,
        film,
        event_type=CREDIT_REMOVED_EVENT_TYPE,
        occurred_at=REMOVED_AT + timedelta(days=20),
        names=["zendaya"],
    )
    await session.commit()

    result = await backfill(session_factory)

    assert (result.removals_read, result.superseded, result.skipped) == (2, 2, 0)
    first_join = await _reload(session, first_join.id)
    rejoin = await _reload(session, rejoin.id)
    assert (first_join.status, first_join.superseded_by) == ("superseded", first_leave.id)
    assert (rejoin.status, rejoin.superseded_by) == ("superseded", second_leave.id)


async def test_backfill_skips_a_removal_with_no_prior_attachment_card(session, session_factory):
    """Nothing to mark is not a failure; the removal is counted as read and left alone."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    await _card(
        session,
        film,
        event_type=CREDIT_REMOVED_EVENT_TYPE,
        occurred_at=REMOVED_AT,
        names=["nobody carded"],
    )
    await session.commit()

    result = await backfill(session_factory)

    assert (result.removals_read, result.superseded, result.skipped, result.failed) == (
        1,
        0,
        0,
        0,
    )
