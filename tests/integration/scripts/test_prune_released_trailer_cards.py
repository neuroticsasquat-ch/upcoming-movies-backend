"""The one-off repair that removes trailer cards on released films (NEU-1532, D-1532.7).

What matters is the blast radius: it runs against production once and is not reversible, so it
must take a released film's catalog trailer card with its summary and digest rows, and leave an
unreleased film's card, a trade-reported trailer, and every `film_video` row exactly where they
are.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select

from scripts.prune_released_trailer_cards import VIDEOS_SHIPPED, prune
from tests.fixtures.catalog import add_film
from upmovies.app.models import Notification
from upmovies.catalog.models import Film, FilmVideo
from upmovies.news.models import Event, EventSummary

# The night the incident card was raised. Stamped explicitly: `created_at` defaults to `now()`,
# and a fixture left to the default would judge "released when carded" against the wall clock.
CARDED = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 10, 3, 17, 0, tzinfo=UTC)


async def _trailer(session, film: Film, *, provenance: str = "catalog", key: str) -> Event:
    """A trailer card on `film`, carded at `CARDED`, with its summary and the ledger row the
    poll recorded for it."""
    assert CARDED >= VIDEOS_SHIPPED
    event = Event(
        film_id=film.id,
        event_type="trailer",
        confidence="confirmed",
        provenance=provenance,
        occurred_at=PUBLISHED,
        subject_key=[f"youtube:{key}"],
        created_at=CARDED,
    )
    session.add(event)
    await session.flush()
    session.add(
        EventSummary(
            event_id=event.id,
            summary="A new trailer is out.",
            model="deterministic",
            prompt_version="v1",
            source_updated_at=CARDED,
        )
    )
    session.add(
        FilmVideo(
            film_id=film.id,
            site="youtube",
            key=key,
            type="Trailer",
            name="Official Trailer",
            published_at=PUBLISHED,
        )
    )
    await session.flush()
    return event


async def _count(session, model, *where) -> int:
    stmt = select(func.count()).select_from(model).where(*where)
    return (await session.execute(stmt)).scalar_one()


async def _fixture(session, make_user) -> tuple[Event, Event, Event, UUID]:
    user = await make_user(email="tom@example.com")
    released = await add_film(session, 9501, status="Released", release_date=date(2026, 7, 29))
    upcoming = await add_film(
        session, 9502, status="Post Production", release_date=date(2027, 6, 1)
    )
    stale = await _trailer(session, released, key="spidey")
    fresh = await _trailer(session, upcoming, key="soon")
    reported = await _trailer(session, released, provenance="story", key="trade")
    for event in (stale, fresh):
        session.add(
            Notification(
                user_id=user.id, event_id=event.id, kind="digest", channel="email", status="sent"
            )
        )
    await session.commit()
    return stale, fresh, reported, user.id


async def test_a_dry_run_reports_the_released_films_card_and_deletes_nothing(session, make_user):
    stale, _, _, user_id = await _fixture(session, make_user)

    pruned = await prune(session, apply=False)

    (card,) = pruned.cards
    assert pruned.deleted == {}

    assert (card.event_id, card.title, card.release_date) == (
        stale.id,
        "Film 9501",
        date(2026, 7, 29),
    )
    assert (card.occurred_at, card.created_at) == (PUBLISHED, CARDED)
    assert card.subject_key == ["youtube:spidey"]
    # By user id: the dry run's output may go on the PR, and emails do not.
    assert [(uid, status) for uid, status, _ in card.notifications] == [(user_id, "sent")]
    assert await _count(session, Event) == 3


async def test_apply_removes_the_card_its_summary_and_its_digest_rows(session, make_user):
    stale, fresh, reported, _ = await _fixture(session, make_user)

    pruned = await prune(session, apply=True)

    assert pruned.deleted == {"event_story": 0, "event_summary": 1, "event": 1}
    assert await _count(session, Event, Event.id == stale.id) == 0
    assert await _count(session, EventSummary, EventSummary.event_id == stale.id) == 0
    assert await _count(session, Notification, Notification.event_id == stale.id) == 0
    # An unreleased film's trailer is exactly what the poll is for.
    assert await _count(session, Event, Event.id == fresh.id) == 1
    assert await _count(session, Notification, Notification.event_id == fresh.id) == 1
    # A trade writing a trailer up is editorial judgement that it is news (D-1532.1).
    assert await _count(session, Event, Event.id == reported.id) == 1
    # The ledger is insert-only, and nothing polls a released film any more to re-card it.
    assert await _count(session, FilmVideo) == 3


async def test_a_film_that_opened_after_its_card_keeps_it(session):
    """Released is judged on the day the card was created: a trailer carded the night before a
    film opened was a genuine beat."""
    film = await add_film(session, 9503, status="Released", release_date=CARDED.date())
    await _trailer(session, film, key="eve")
    await session.commit()

    assert (await prune(session, apply=False)).cards == []
