"""The one-off repair that removes admission cards on released films (NEU-1505, D-1505.8).

What matters is the blast radius: it runs against production once and is not reversible, so it
must take a released film's admission card, its change row and its digest rows, and leave an
unreleased film's admission card — the beat EF-4 exists for — exactly where it is.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select

from scripts.prune_admission_cards import ADMISSION_SHIPPED, DEFAULT_CUTOFF, prune
from tests.fixtures.catalog import add_film
from upmovies.app.models import Follow, Notification
from upmovies.catalog.models import Film, FilmCreditChange, Person
from upmovies.news.models import Event, EventSummary

# The night of the incident import. Fixture rows are stamped with it explicitly: every column
# involved defaults to `now()`, and a fixture left to the default drifts out of the window the
# day the wall clock passes the cutoff (the NEU-1121 test learned this the hard way).
NOW = datetime(2026, 9, 26, 23, 40, tzinfo=UTC)
FINCHER = 7467


async def _admitted(session, tmdb_id: int, *, release_date: date) -> tuple[Film, Event]:
    """A film first observed at `NOW` with a followed director on it, and the card that
    admission row became."""
    assert ADMISSION_SHIPPED <= NOW < DEFAULT_CUTOFF
    film = await add_film(
        session, tmdb_id, release_date=release_date, created_at=NOW, credits_observed_at=NOW
    )
    if await session.get(Person, FINCHER) is None:
        session.add(Person(id=FINCHER, name="David Fincher"))
        await session.flush()
    session.add(
        FilmCreditChange(
            film_id=film.id,
            person_id=FINCHER,
            credit_type="crew",
            job="Director",
            change="added",
            changed_at=NOW,
        )
    )
    event = Event(
        film_id=film.id,
        event_type="crew_attached",
        confidence="rumored",
        provenance="catalog",
        occurred_at=NOW,
        subject_key=["david fincher"],
    )
    session.add(event)
    await session.flush()
    session.add(
        EventSummary(
            event_id=event.id,
            summary="David Fincher attached to direct.",
            model="deterministic",
            prompt_version="v1",
            source_updated_at=NOW,
        )
    )
    await session.flush()
    return film, event


async def _count(session, model, *where) -> int:
    stmt = select(func.count()).select_from(model).where(*where)
    return (await session.execute(stmt)).scalar_one()


async def _fixture(session, make_user) -> tuple[Event, Event, UUID]:
    user = await make_user(email="tom@example.com")
    _, mank = await _admitted(session, 9401, release_date=date(2020, 12, 4))
    _, upcoming = await _admitted(session, 9402, release_date=date(2027, 6, 1))
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id=str(FINCHER), source="manual")
    )
    for event in (mank, upcoming):
        session.add(
            Notification(
                user_id=user.id, event_id=event.id, kind="digest", channel="email", status="sent"
            )
        )
    await session.commit()
    return mank, upcoming, user.id


async def test_a_dry_run_reports_the_released_films_card_and_deletes_nothing(session, make_user):
    mank, _, user_id = await _fixture(session, make_user)

    pruned = await prune(session, apply=False)

    (card,) = pruned.cards
    assert (card.event_id, card.title) == (mank.id, "Film 9401")
    # By user id: the dry run's output goes on the PR, and emails do not.
    assert [(uid, status) for uid, status, _ in card.notifications] == [(user_id, "sent")]
    assert card.followers == {user_id}
    assert len(pruned.change_rows[FilmCreditChange]) == 1
    assert await _count(session, Event) == 2
    assert await _count(session, FilmCreditChange) == 2


async def test_apply_removes_the_card_its_change_row_and_its_digest_rows(session, make_user):
    mank, upcoming, _ = await _fixture(session, make_user)
    mank_film_id = mank.film_id

    await prune(session, apply=True)

    assert await _count(session, Event, Event.id == mank.id) == 0
    assert await _count(session, EventSummary, EventSummary.event_id == mank.id) == 0
    assert await _count(session, Notification, Notification.event_id == mank.id) == 0
    assert await _count(session, FilmCreditChange, FilmCreditChange.film_id == mank_film_id) == 0
    # The unreleased film's admission is exactly what EF-4 is for, and survives whole.
    assert await _count(session, Event, Event.id == upcoming.id) == 1
    assert await _count(session, Notification, Notification.event_id == upcoming.id) == 1
    assert (
        await _count(session, FilmCreditChange, FilmCreditChange.film_id == upcoming.film_id) == 1
    )


async def test_a_card_from_an_ordinary_diff_is_untouched(session, make_user):
    # Same released film, but the card is dated after the marker: a director who attached on
    # a later ingest, which the gate never touched.
    mank, _, _ = await _fixture(session, make_user)
    later = Event(
        film_id=mank.film_id,
        event_type="crew_attached",
        confidence="rumored",
        provenance="catalog",
        occurred_at=datetime(2026, 9, 27, 6, 0, tzinfo=UTC),
    )
    session.add(later)
    await session.commit()

    await prune(session, apply=True)

    assert await _count(session, Event, Event.id == later.id) == 1


async def test_a_film_admitted_unreleased_that_has_opened_since_keeps_its_card(session):
    # Admitted the night before it opened: a genuine EF-4 beat. "Released" is judged against
    # the admission's own date, so the film opening since does not make its card a mistake.
    _, card = await _admitted(session, 9403, release_date=date(2026, 9, 27))
    await session.commit()

    assert (await prune(session, apply=False)).cards == []
