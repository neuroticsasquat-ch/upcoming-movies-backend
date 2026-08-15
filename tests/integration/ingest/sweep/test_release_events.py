"""The sweep's release-date phase end to end (NEU-1121).

Two families of rule live here. The first is what makes this phase exist at all: the card is
about a **displayable** date — US or origin country, theatrical — so it names something the
film page actually lists, and several markets moving in one observation share one card.

The second moved here wholesale from `field_events` when release dates did: the ADR-0002
anti-double-card rule. A story-triggered release-date event and a catalog-triggered one can
read the same move, and must never both card it. Its mirror is `link.cluster`'s dedup target;
both must move together or one direction reopens.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import FilmReleaseDateChange
from upmovies.ingest.models import IngestRun
from upmovies.ingest.sweep import release_events, run_release_date_events
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL, TEMPLATE_VERSION

NOW = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
LOOKBACK_DAYS = 7
WINDOW_DAYS = 14


async def _change(
    session,
    film,
    *,
    region="US",
    release_type=3,
    previous=None,
    new=date(2026, 12, 4),
    change="set",
    changed_at=YESTERDAY,
):
    session.add(
        FilmReleaseDateChange(
            film_id=film.id,
            iso_3166_1=region,
            release_type=release_type,
            previous_date=previous,
            new_date=new,
            change=change,
            changed_at=changed_at,
        )
    )
    await session.flush()


async def _run(session_factory, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "run_id": run_id,
        "now": NOW,
        "lookback_days": LOOKBACK_DAYS,
        "corroboration_window_days": WINDOW_DAYS,
    }
    return await run_release_date_events(**{**kwargs, **overrides})


async def _events(session, film):
    return (
        (
            await session.execute(
                select(Event).where(Event.film_id == film.id).order_by(Event.occurred_at),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


async def _summary(session, event):
    return (
        await session.execute(select(EventSummary).where(EventSummary.event_id == event.id))
    ).scalar_one()


async def test_a_set_date_cards_a_summarized_region_tagged_event(session, session_factory, run_id):
    film = await add_film(session, 1, title="Runner")
    await _change(session, film)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created, result.skipped) == (1, 1, 0)
    (event,) = await _events(session, film)
    assert event.event_type == "release_date"
    assert event.provenance == "catalog"
    assert event.confidence == "confirmed"
    # The observation's own timestamp, so a backlog worked off after an outage is not all
    # dated today.
    assert event.occurred_at == YESTERDAY
    # Region is populated now, so `_region_visible()` finally applies to catalog events.
    assert event.region == "US"
    assert event.subject_key == ["US:wide"]
    summary = await _summary(session, event)
    assert summary.summary == "US wide release date set to 4 December 2026."
    assert summary.model == DETERMINISTIC_MODEL
    assert summary.prompt_version == TEMPLATE_VERSION


async def test_a_moved_date_names_both_dates_and_the_market(session, session_factory, run_id):
    film = await add_film(session, 1)
    await _change(
        session,
        film,
        release_type=2,
        previous=date(2026, 8, 14),
        new=date(2026, 12, 4),
        change="moved",
    )
    await session.commit()

    await _run(session_factory, run_id)

    (event,) = await _events(session, film)
    summary = await _summary(session, event)
    assert summary.summary == (
        "US limited release date moved from 14 August 2026 to 4 December 2026."
    )
    assert event.subject_key == ["US:limited"]


async def test_two_markets_in_one_observation_share_one_card(session, session_factory, run_id):
    """`uq_event_catalog_change` allows one catalog event per film, type and timestamp, and a
    distributor shifting limited and wide at once is one beat, not two."""
    film = await add_film(session, 1)
    await _change(session, film, release_type=3, new=date(2028, 1, 15))
    await _change(session, film, release_type=2, new=date(2028, 1, 8))
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert sorted(event.subject_key) == ["US:limited", "US:wide"]
    summary = await _summary(session, event)
    assert "wide release date set to 15 January 2028." in summary.summary
    assert "limited release date set to 8 January 2028." in summary.summary


async def test_separate_observations_stay_separate_cards(session, session_factory, run_id):
    """Grouping is keyed on the observation timestamp, not on the run — two reads of the same
    film days apart are two beats however close together the sweep gets to them."""
    film = await add_film(session, 1)
    await _change(session, film, new=date(2026, 11, 20), changed_at=NOW - timedelta(days=4))
    await _change(
        session,
        film,
        previous=date(2026, 11, 20),
        new=date(2026, 12, 4),
        change="moved",
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 2
    assert len(await _events(session, film)) == 2


async def test_an_origin_country_date_is_tagged_with_its_own_region(
    session, session_factory, run_id
):
    film = await add_film(session, 1)
    await _change(session, film, region="GB")
    await session.commit()

    await _run(session_factory, run_id)

    (event,) = await _events(session, film)
    assert event.region == "GB"
    assert event.subject_key == ["GB:wide"]


async def test_a_group_spanning_markets_is_tagged_us(session, session_factory, run_id):
    """`Event.region` holds one value and `US` is in every film's displayable set, so tagging
    the group `US` keeps it visible for exactly the audience the page's list serves."""
    film = await add_film(session, 1)
    await _change(session, film, region="GB")
    await _change(session, film, region="US")
    await session.commit()

    await _run(session_factory, run_id)

    (event,) = await _events(session, film)
    assert event.region == "US"


async def test_a_second_pass_over_the_same_window_cards_nothing_new(
    session, session_factory, run_id
):
    film = await add_film(session, 1)
    await _change(session, film)
    await session.commit()

    first = await _run(session_factory, run_id)
    second = await _run(session_factory, run_id)

    assert first.events_created == 1
    assert (second.events_created, second.skipped) == (0, 1)
    assert len(await _events(session, film)) == 1


async def test_changes_older_than_the_lookback_are_never_read(session, session_factory, run_id):
    film = await add_film(session, 1)
    await _change(session, film, changed_at=NOW - timedelta(days=LOOKBACK_DAYS + 1))
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.changes_read, result.events_created) == (0, 0)
    assert await _events(session, film) == []


async def test_a_story_event_for_the_same_move_is_not_carded_twice(
    session, session_factory, run_id
):
    """ADR-0002's path ran first — `link` corroborated a held story against this very move and
    carded it, dating the event to the story rather than to the change. Carding it again here
    would put two release-date cards on the film for one move. Moved from `field_events` with
    the dates themselves (NEU-1121)."""
    film = await add_film(session, 1)
    await _change(session, film)
    session.add(
        Event(
            film_id=film.id,
            event_type="release_date",
            confidence="confirmed",
            occurred_at=YESTERDAY - timedelta(days=2),
        )
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.events_created, result.skipped) == (0, 1)
    assert len(await _events(session, film)) == 1


async def test_a_story_event_outside_the_window_does_not_suppress_the_card(
    session, session_factory, run_id
):
    film = await add_film(session, 1)
    await _change(session, film)
    session.add(
        Event(
            film_id=film.id,
            event_type="release_date",
            confidence="confirmed",
            occurred_at=YESTERDAY - timedelta(days=WINDOW_DAYS + 1),
        )
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    assert len(await _events(session, film)) == 2


async def test_each_film_commits_on_its_own(session, session_factory, run_id, monkeypatch):
    """Commit per item: one film whose write blows up must not cost the others."""
    first = await add_film(session, 1)
    second = await add_film(session, 2)
    await _change(session, first, new=date(2026, 12, 4))
    await _change(session, second, new=date(2026, 12, 5), changed_at=NOW - timedelta(hours=1))
    await session.commit()

    real = release_events.write_deterministic_summary

    async def explode(session_, *, event_id, change, source_updated_at):
        if any(c.new_date == date(2026, 12, 4) for c in change.changes):
            raise RuntimeError("summary write failed")
        return await real(
            session_, event_id=event_id, change=change, source_updated_at=source_updated_at
        )

    monkeypatch.setattr(release_events, "write_deterministic_summary", explode)

    result = await _run(session_factory, run_id)

    assert (result.events_created, result.failures) == (1, 1)
    assert await _events(session, first) == []
    assert len(await _events(session, second)) == 1


async def test_consecutive_failures_abort_the_phase(session, session_factory, run_id, monkeypatch):
    for i in range(1, 4):
        film = await add_film(session, i)
        await _change(session, film, changed_at=NOW - timedelta(hours=i))
    await session.commit()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(release_events, "write_deterministic_summary", explode)

    result = await _run(session_factory, run_id, failure_threshold=2)

    assert result.aborted is True
    assert result.abort_error == "aborted after 2 consecutive failures"
    assert result.failures == 2


async def test_the_run_counts_what_it_carded(session, session_factory, run_id):
    film = await add_film(session, 1)
    await _change(session, film)
    await session.commit()

    await _run(session_factory, run_id)

    run = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert run.items_processed == 1
