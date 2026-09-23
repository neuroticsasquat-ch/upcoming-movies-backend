"""Admission to card, end to end (EF-4, NEU-1436).

Every other test of this ticket stops at the history row. These run the whole beat: a film is
admitted through `upsert_film` with a followed person, studio or franchise already on it, the
clock is advanced past the credit quarantine, and the ordinary carding phase runs. Nothing in
those phases was changed for EF-4 — that is the claim under test. An admission row has to be
judged by the same quarantine, the same presence check and the same summary writer as a row
the diff wrote, or "ordinary cards" is a docstring rather than a property.

The clock is advanced rather than the rows backdated, because `upsert_film` stamps
`changed_at` from the database's own `now()` and the point is to exercise the row it really
writes.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow
from upmovies.catalog.models import FilmCreditChange
from upmovies.ingest.sweep import (
    run_collection_events,
    run_company_events,
    run_credit_attachment_events,
)
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film
from upmovies.news.models import Event, EventSummary

QUARANTINE_HOURS = 72
LOOKBACK_DAYS = 7
DUNE = {"id": 726871, "name": "Dune Collection"}


def _after_quarantine() -> datetime:
    """Far enough past the row's `changed_at` to clear the hold, near enough to stay inside
    the phase's rolling window."""
    return datetime.now(UTC) + timedelta(hours=QUARANTINE_HOURS + 1)


def _crew(person_id: int, job: str, department: str = "Directing") -> dict:
    return {
        "id": person_id,
        "name": f"Person {person_id}",
        "credit_id": f"crew-{person_id}-{job}",
        "job": job,
        "department": department,
    }


def _details(tmdb_id: int, **overrides) -> TMDBMovieDetails:
    payload = {"status": "Planned", "release_date": None, **overrides}
    return TMDBMovieDetails.model_validate(make_details(tmdb_id, **payload))


async def _follow(session, user, entity_type: str, entity_id: int) -> Follow:
    follow = Follow(
        user_id=user.id, entity_type=entity_type, entity_id=str(entity_id), source="manual"
    )
    session.add(follow)
    await session.commit()
    return follow


async def _events(session) -> list[Event]:
    result = await session.execute(
        select(Event).order_by(Event.event_type),
        execution_options={"populate_existing": True},
    )
    return list(result.scalars().all())


async def _summary(session, event) -> EventSummary:
    return (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one()


async def test_a_followed_director_on_a_new_film_cards_an_ordinary_crew_event(
    session, session_factory, run_id, make_user
):
    """The whole point of the ticket, from admission to card. Nothing about the resulting
    event says it came from a first observation: catalog-sourced, `rumored`, `occurred_at` at
    the observation, and the deterministic summary every other attachment gets."""
    user = await make_user(email="follower@example.com")
    await _follow(session, user, "person", 100)

    await upsert_film(
        session, _details(9300, credits={"cast": [], "crew": [_crew(100, "Director")]})
    )
    await session.commit()

    result = await run_credit_attachment_events(
        session_factory=session_factory,
        run_id=run_id,
        now=_after_quarantine(),
        lookback_days=LOOKBACK_DAYS,
        quarantine_hours=QUARANTINE_HOURS,
    )

    assert (result.attachments_read, result.events_created) == (1, 1)
    (event,) = await _events(session)
    assert event.event_type == "crew_attached"
    assert event.provenance == "catalog"
    assert event.confidence == "rumored"
    assert event.subject_key == ["person 100"]
    assert (await _summary(session, event)).summary == "Person 100 attached to direct."
    # The beat is dated at the observation, not at the pass that carded it — which is what
    # puts the card under the right day on the film page (NEU-1204) even though the hold
    # delayed it by three.
    change = (
        await session.execute(
            select(FilmCreditChange), execution_options={"populate_existing": True}
        )
    ).scalar_one()
    assert event.occurred_at == change.changed_at


async def test_the_same_film_with_nobody_following_cards_nothing(session, session_factory, run_id):
    """The baseline rule, still doing its job for everybody else. This is the assertion that
    would catch EF-4 widening into the catalog-wide flood it exists inside."""
    await upsert_film(
        session, _details(9301, credits={"cast": [], "crew": [_crew(100, "Director")]})
    )
    await session.commit()

    result = await run_credit_attachment_events(
        session_factory=session_factory,
        run_id=run_id,
        now=_after_quarantine(),
        lookback_days=LOOKBACK_DAYS,
        quarantine_hours=QUARANTINE_HOURS,
    )

    assert (result.attachments_read, result.events_created) == (0, 0)
    assert await _events(session) == []


async def test_the_admission_row_is_still_held_by_quarantine(
    session, session_factory, run_id, make_user
):
    """An admission row is quarantined like any other (D-1436.8): run the phase before the
    hold clears and the card does not publish."""
    user = await make_user(email="follower@example.com")
    await _follow(session, user, "person", 100)

    await upsert_film(
        session, _details(9302, credits={"cast": [], "crew": [_crew(100, "Director")]})
    )
    await session.commit()

    result = await run_credit_attachment_events(
        session_factory=session_factory,
        run_id=run_id,
        now=datetime.now(UTC) + timedelta(hours=1),
        lookback_days=LOOKBACK_DAYS,
        quarantine_hours=QUARANTINE_HOURS,
    )

    assert result.events_created == 0
    assert await _events(session) == []


async def test_unfollowing_inside_the_window_drops_a_non_seed_admission(
    session, session_factory, run_id, make_user
):
    """D-1436.8's accepted cost, pinned. The gate asks whether the credit is still
    *recorded*, and a gaffer is recorded only through the follow — so dropping the follow
    before the hold clears ages the pending card out uncarded.

    It is the follow that has to go, not the credit: a followed *director*'s admission row
    would still card here, because a director is seed grade whoever follows them.
    """
    user = await make_user(email="follower@example.com")
    follow = await _follow(session, user, "person", 101)

    await upsert_film(
        session,
        _details(9303, credits={"cast": [], "crew": [_crew(101, "Gaffer", "Lighting")]}),
    )
    await session.commit()

    await session.delete(follow)
    await session.commit()

    result = await run_credit_attachment_events(
        session_factory=session_factory,
        run_id=run_id,
        now=_after_quarantine(),
        lookback_days=LOOKBACK_DAYS,
        quarantine_hours=QUARANTINE_HOURS,
    )

    assert result.events_created == 0
    assert await _events(session) == []


async def test_a_followed_studio_on_a_new_film_cards_a_company_event(
    session, session_factory, run_id, make_user
):
    """The studio variant, through NEU-1433's phase unchanged."""
    user = await make_user(email="follower@example.com")
    await _follow(session, user, "company", 33)

    await upsert_film(
        session,
        _details(
            9304,
            production_companies=[{"id": 33, "name": "Legendary Pictures"}],
        ),
    )
    await session.commit()

    result = await run_company_events(
        session_factory=session_factory,
        run_id=run_id,
        now=_after_quarantine(),
        lookback_days=LOOKBACK_DAYS,
        quarantine_hours=QUARANTINE_HOURS,
    )

    assert result.events_created == 1
    (event,) = await _events(session)
    assert event.event_type == "company_attached"
    assert event.provenance == "catalog"
    assert event.confidence == "rumored"


async def test_a_followed_franchise_on_a_new_film_cards_a_collection_event(
    session, session_factory, run_id, make_user
):
    """The franchise variant, through NEU-1434's phase unchanged — which is the whole reason
    the admission path writes a `film_field_change` row rather than carding for itself."""
    user = await make_user(email="follower@example.com")
    await _follow(session, user, "franchise", DUNE["id"])

    await upsert_film(session, _details(9305, belongs_to_collection=DUNE))
    await session.commit()

    result = await run_collection_events(
        session_factory=session_factory,
        run_id=run_id,
        now=_after_quarantine(),
        lookback_days=LOOKBACK_DAYS,
        quarantine_hours=QUARANTINE_HOURS,
    )

    assert result.events_created == 1
    (event,) = await _events(session)
    assert event.event_type == "collection_attached"
    assert event.provenance == "catalog"
    assert event.confidence == "rumored"
