"""NEU-968: an event's `occurred_at` must follow its surviving stories. It is stamped once
at creation from the founding group's earliest story; when repair removes that story the
timestamp has to be recomputed, or the event keeps claiming a date no story supports."""

from datetime import UTC, datetime

from upmovies.catalog.models import Film
from upmovies.link.event_repair import (
    reject_stories_and_repair_events,
    repair_drifted_occurred_at,
)
from upmovies.news.models import Event, EventStory, Story


def _at(day: int, *, month: int = 7) -> datetime:
    return datetime(2026, month, day, 12, 0, tzinfo=UTC)


async def _film(session, slug):
    film = Film(tmdb_id=abs(hash(slug)) % 10_000_000, slug=slug, title="F")
    session.add(film)
    await session.flush()
    return film


async def _story(session, film_id, url, *, published_at=None, fetched_at=None):
    s = Story(
        source="x",
        url=url,
        title="t",
        film_id=film_id,
        link_status="linked",
        published_at=published_at,
        fetched_at=fetched_at or datetime.now(UTC),
    )
    session.add(s)
    await session.flush()
    return s


async def _event(session, film_id, story_ids, *, occurred_at):
    now = datetime.now(UTC)
    ev = Event(
        film_id=film_id,
        event_type="trailer",
        confidence="confirmed",
        occurred_at=occurred_at,
        updated_at=now,
    )
    session.add(ev)
    await session.flush()
    for sid in story_ids:
        session.add(EventStory(event_id=ev.id, story_id=sid))
    await session.flush()
    return ev


async def test_recomputes_occurred_at_when_the_founding_story_is_rejected(session):
    film = await _film(session, "er1")
    early = await _story(session, film.id, "https://a.com/1", published_at=_at(1))
    late = await _story(session, film.id, "https://b.com/2", published_at=_at(10))
    ev = await _event(session, film.id, [early.id, late.id], occurred_at=_at(1))

    report = await reject_stories_and_repair_events(
        session, [early], link_note="source-blocked", apply=True
    )

    assert report.events_deleted == 0
    assert report.events_resummarized == 1
    kept = await session.get(Event, ev.id)
    assert kept is not None
    assert kept.occurred_at == _at(10)


async def test_leaves_occurred_at_alone_when_the_earliest_story_survives(session):
    film = await _film(session, "er2")
    early = await _story(session, film.id, "https://a.com/3", published_at=_at(1))
    late = await _story(session, film.id, "https://b.com/4", published_at=_at(10))
    ev = await _event(session, film.id, [early.id, late.id], occurred_at=_at(1))

    await reject_stories_and_repair_events(session, [late], link_note="source-blocked", apply=True)

    kept = await session.get(Event, ev.id)
    assert kept is not None
    assert kept.occurred_at == _at(1)


async def test_falls_back_to_fetched_at_when_the_survivor_has_no_published_at(session):
    film = await _film(session, "er3")
    early = await _story(session, film.id, "https://a.com/5", published_at=_at(1))
    late = await _story(session, film.id, "https://b.com/6", published_at=None, fetched_at=_at(9))
    ev = await _event(session, film.id, [early.id, late.id], occurred_at=_at(1))

    await reject_stories_and_repair_events(session, [early], link_note="source-blocked", apply=True)

    kept = await session.get(Event, ev.id)
    assert kept is not None
    assert kept.occurred_at == _at(9)


async def test_dry_run_leaves_occurred_at_untouched(session):
    film = await _film(session, "er4")
    early = await _story(session, film.id, "https://a.com/7", published_at=_at(1))
    late = await _story(session, film.id, "https://b.com/8", published_at=_at(10))
    ev = await _event(session, film.id, [early.id, late.id], occurred_at=_at(1))

    await reject_stories_and_repair_events(
        session, [early], link_note="source-blocked", apply=False
    )

    kept = await session.get(Event, ev.id)
    assert kept is not None
    assert kept.occurred_at == _at(1)


async def test_backfill_pulls_a_drifted_event_forward_to_its_earliest_story(session):
    film = await _film(session, "er5")
    s = await _story(session, film.id, "https://a.com/9", published_at=_at(13))
    ev = await _event(session, film.id, [s.id], occurred_at=_at(1))  # 12 days too early

    report = await repair_drifted_occurred_at(session, apply=True)

    assert report.events_repaired == 1
    assert report.max_drift_days == 12
    repaired = await session.get(Event, ev.id)
    assert repaired is not None
    assert repaired.occurred_at == _at(13)


async def test_backfill_is_idempotent(session):
    film = await _film(session, "er6")
    s = await _story(session, film.id, "https://a.com/10", published_at=_at(13))
    await _event(session, film.id, [s.id], occurred_at=_at(1))

    await repair_drifted_occurred_at(session, apply=True)
    second = await repair_drifted_occurred_at(session, apply=True)

    assert second.events_repaired == 0
    assert second.max_drift_days == 0


async def test_backfill_ignores_an_event_later_than_its_earliest_story(session):
    """An `occurred_at` later than the earliest story means a story joined an existing beat.
    That must not drag the beat's date forward, so the backfill leaves it alone."""
    film = await _film(session, "er7")
    s = await _story(session, film.id, "https://a.com/11", published_at=_at(1))
    ev = await _event(session, film.id, [s.id], occurred_at=_at(5))

    report = await repair_drifted_occurred_at(session, apply=True)

    assert report.events_repaired == 0
    unchanged = await session.get(Event, ev.id)
    assert unchanged is not None
    assert unchanged.occurred_at == _at(5)


async def test_backfill_dry_run_reports_but_changes_nothing(session):
    film = await _film(session, "er8")
    s = await _story(session, film.id, "https://a.com/12", published_at=_at(13))
    ev = await _event(session, film.id, [s.id], occurred_at=_at(1))

    report = await repair_drifted_occurred_at(session, apply=False)

    assert report.events_repaired == 1
    assert report.max_drift_days == 12
    untouched = await session.get(Event, ev.id)
    assert untouched is not None
    assert untouched.occurred_at == _at(1)
