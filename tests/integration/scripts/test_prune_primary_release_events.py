"""The one-off repair that removes primary-date release events (NEU-1121).

What matters is the blast radius: it must take every catalog release-date event and nothing
else, since it runs against production once and is not reversible.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select

from scripts.prune_primary_release_events import DEFAULT_CUTOFF, prune
from tests.fixtures.catalog import add_film
from upmovies.news.models import Event, EventSummary

NOW = datetime(2026, 8, 11, 5, 11, tzinfo=UTC)


async def _event(session, film, *, event_type="release_date", provenance="catalog"):
    # `created_at`, not `occurred_at`: the cutoff is a *deploy* boundary, so `prune` selects on
    # when the row was written. It is set explicitly because the column is `server_default
    # now()` — left to the default, a fixture row is stamped at real insert time, and these
    # tests silently stopped selecting anything the moment the wall clock passed the cutoff on
    # 2026-08-12. The guard below pinned `occurred_at`, which the query never reads, so it
    # gave the appearance of protection while the actual window went unchecked.
    assert NOW < DEFAULT_CUTOFF  # the fixtures below must land inside the pruned window
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="confirmed",
        provenance=provenance,
        occurred_at=NOW,
        created_at=NOW,
    )
    session.add(event)
    await session.flush()
    session.add(
        EventSummary(
            event_id=event.id,
            summary="Release date moved from 16 December 2027 to 15 September 2027.",
            model="deterministic",
            prompt_version="v1",
            source_updated_at=NOW,
        )
    )
    await session.flush()
    return event


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_a_dry_run_deletes_nothing(session):
    film = await add_film(session, 1)
    await _event(session, film)
    await session.commit()

    assert await prune(session, apply=False) == 1
    assert await _count(session, Event) == 1


async def test_apply_removes_the_event_and_its_summary(session):
    film = await add_film(session, 1)
    await _event(session, film)
    await session.commit()

    assert await prune(session, apply=True) == 1
    assert await _count(session, Event) == 0
    # Cleared explicitly: an orphaned summary is invisible to every read path but still there.
    assert await _count(session, EventSummary) == 0


async def test_story_sourced_release_date_events_are_untouched(session):
    """`link` reading a trade report is a different, working path — NEU-1121 says nothing
    about it."""
    film = await add_film(session, 1)
    await _event(session, film, provenance="story")
    await session.commit()

    assert await prune(session, apply=True) == 0
    assert await _count(session, Event) == 1


async def test_other_catalog_event_types_are_untouched(session):
    film = await add_film(session, 1)
    await _event(session, film, event_type="production_start")
    await _event(session, film, event_type="casting")
    await session.commit()

    assert await prune(session, apply=True) == 0
    assert await _count(session, Event) == 2


async def test_it_takes_every_catalog_release_event_across_films(session):
    first = await add_film(session, 1)
    second = await add_film(session, 2)
    await _event(session, first)
    await _event(session, second)
    await _event(session, second, event_type="production_wrap")
    await session.commit()

    assert await prune(session, apply=True) == 2
    assert await _count(session, Event) == 1


async def test_events_created_after_the_cutoff_are_left_alone(session):
    """The bound is the whole safety of running this twice: events carded after the fix
    ships come from `film_release_date_change` and are correct."""
    film = await add_film(session, 1)
    event = await _event(session, film)
    event.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    await session.commit()

    assert await prune(session, apply=True) == 0
    assert await _count(session, Event) == 1
