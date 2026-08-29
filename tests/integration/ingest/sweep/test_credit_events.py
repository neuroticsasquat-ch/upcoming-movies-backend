"""The sweep's credit-attachment phase end to end: which `catalog.film_credit_change` rows
become events, and what stops the same attachment becoming two cards.

The rule carrying the most weight is the one that is *not* implemented here at all — first
observation is a baseline (§5.3), which NEU-1082 guarantees upstream by writing no history
row for a film's first credit set. It is asserted anyway, because this is the phase where
getting it wrong would surface: tens of thousands of false "attached to direct" cards on the
first day the expansion ran.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import FilmCreditChange, Person
from upmovies.ingest.sweep import credit_events, run_credit_attachment_events
from upmovies.ingest.sweep.credit_events import run_credit_detachment_events
from upmovies.ingest.tmdb.credit_history import (
    CREDIT_ADDED,
    CREDIT_REMOVED,
    SeedCredit,
    diff_seed_credits,
    record_credit_changes,
)
from upmovies.news.catalog_events import CREDIT_REMOVED_EVENT_TYPE
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL, TEMPLATE_VERSION

NOW = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
LOOKBACK_DAYS = 7


async def _person(session, person_id: int, name: str) -> Person:
    person = Person(id=person_id, name=name)
    session.add(person)
    await session.flush()
    return person


async def _attached(
    session,
    film,
    person,
    *,
    credit_type="crew",
    job: str | None = "Director",
    change=CREDIT_ADDED,
    changed_at=YESTERDAY,
):
    session.add(
        FilmCreditChange(
            film_id=film.id,
            person_id=person.id,
            credit_type=credit_type,
            job=job,
            change=change,
            changed_at=changed_at,
        )
    )
    await session.flush()


async def _cast(session, film, person, *, changed_at=YESTERDAY, change=CREDIT_ADDED):
    await _attached(
        session,
        film,
        person,
        credit_type="cast",
        job=None,
        change=change,
        changed_at=changed_at,
    )


async def _run(session_factory, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "run_id": run_id,
        "now": NOW,
        "lookback_days": LOOKBACK_DAYS,
    }
    return await run_credit_attachment_events(**{**kwargs, **overrides})


async def _run_detachment(session_factory, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "run_id": run_id,
        "now": NOW,
        "lookback_days": LOOKBACK_DAYS,
    }
    return await run_credit_detachment_events(**{**kwargs, **overrides})


async def _events(session, film):
    return (
        (
            await session.execute(
                select(Event).where(Event.film_id == film.id).order_by(Event.event_type),
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


async def test_a_director_attaching_cards_a_rumored_crew_event(session, session_factory, run_id):
    """The card the project was largely built for: a director attached to an undated film,
    with no trade story anywhere."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.attachments_read, result.events_created, result.skipped) == (1, 1, 0)
    (event,) = await _events(session, film)
    assert event.event_type == "crew_attached"
    assert event.provenance == "catalog"
    # Below the field-change phase's `confirmed`: TMDB is community-edited, and a credit an
    # anonymous editor added is not a studio announcement (§5.4).
    assert event.confidence == "rumored"
    assert event.occurred_at == YESTERDAY
    assert event.subject_key == ["denis villeneuve"]
    summary = await _summary(session, event)
    assert summary.summary == "Denis Villeneuve attached to direct."
    assert summary.model == DETERMINISTIC_MODEL
    assert summary.prompt_version == TEMPLATE_VERSION


async def test_a_writer_attaching_cards_the_same_type(session, session_factory, run_id):
    film = await add_film(session, 1)
    person = await _person(session, 100, "Jon Spaihts")
    await _attached(session, film, person, job="Screenplay")
    await session.commit()

    await _run(session_factory, run_id)

    (event,) = await _events(session, film)
    assert event.event_type == "crew_attached"
    assert (await _summary(session, event)).summary == "Jon Spaihts attached to write."


async def test_cast_added_in_one_pass_card_as_one_event_naming_all_of_them(
    session, session_factory, run_id
):
    """Three performers arriving between two ingests is one beat. Three cards would be the
    single most visible way this phase could go wrong on a busy day."""
    film = await add_film(session, 1)
    for person_id, name in ((1, "Timothée Chalamet"), (2, "Zendaya"), (3, "Rebecca Ferguson")):
        await _cast(session, film, await _person(session, person_id, name))
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.attachments_read, result.events_created) == (3, 1)
    (event,) = await _events(session, film)
    assert event.event_type == "casting"
    assert event.confidence == "rumored"
    assert event.subject_key == ["timothée chalamet", "zendaya", "rebecca ferguson"]
    assert (await _summary(session, event)).summary == (
        "Timothée Chalamet, Zendaya and Rebecca Ferguson join the cast."
    )


async def test_crew_and_cast_in_one_pass_card_as_their_own_beats(session, session_factory, run_id):
    film = await add_film(session, 1)
    await _attached(session, film, await _person(session, 1, "Denis Villeneuve"))
    await _cast(session, film, await _person(session, 2, "Zendaya"))
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 2
    assert [e.event_type for e in await _events(session, film)] == ["casting", "crew_attached"]


async def test_a_films_first_observed_credits_card_nothing(session, session_factory, run_id):
    """§5.3, the headline rule. Guaranteed upstream — `diff_seed_credits` returns nothing for
    a film the catalog has never observed — so this drives the *real* writer rather than
    hand-inserting rows, which would assert nothing about production behaviour."""
    film = await add_film(session, 1)
    for person_id, name in ((1, "Denis Villeneuve"), (2, "Zendaya")):
        await _person(session, person_id, name)
    first_observation = diff_seed_credits(
        previous=None,
        current=[
            SeedCredit(person_id=1, credit_type="crew", job="Director"),
            SeedCredit(person_id=2, credit_type="cast", job=None),
        ],
    )
    await record_credit_changes(session, film.id, first_observation)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.attachments_read, result.events_created) == (0, 0)
    assert await _events(session, film) == []


async def test_a_second_pass_over_the_same_window_cards_nothing_new(
    session, session_factory, run_id
):
    """The window is a fixed rolling one, so every attachment is re-read for days. This is
    the property that keeps that free."""
    film = await add_film(session, 1)
    await _attached(session, film, await _person(session, 1, "Denis Villeneuve"))
    await _cast(session, film, await _person(session, 2, "Zendaya"))
    await session.commit()

    first = await _run(session_factory, run_id)
    second = await _run(session_factory, run_id)

    assert first.events_created == 2
    assert (second.events_created, second.skipped) == (0, 2)
    assert len(await _events(session, film)) == 2


async def test_a_later_observation_of_the_same_film_cards_again(session, session_factory, run_id):
    """Two observations are two beats. The skip is keyed on the observation's timestamp, not
    on the film — a second performer arriving on Tuesday is not the Monday card."""
    film = await add_film(session, 1)
    await _cast(
        session, film, await _person(session, 1, "Zendaya"), changed_at=NOW - timedelta(days=3)
    )
    await _cast(session, film, await _person(session, 2, "Josh Brolin"))
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 2
    assert [e.occurred_at for e in await _events(session, film)] == sorted(
        [NOW - timedelta(days=3), YESTERDAY]
    )


async def test_a_detachment_is_history_but_not_a_beat(session, session_factory, run_id):
    """A credit leaving is what makes a later re-attachment a change again, but "no longer
    attached" is not a card — mostly it is TMDB reverting its own vandalism."""
    film = await add_film(session, 1)
    person = await _person(session, 1, "Denis Villeneuve")
    await _attached(session, film, person, change=CREDIT_REMOVED)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.attachments_read, result.events_created) == (0, 0)
    assert await _events(session, film) == []


async def test_attachments_older_than_the_lookback_are_never_read(session, session_factory, run_id):
    film = await add_film(session, 1)
    await _attached(
        session,
        film,
        await _person(session, 1, "Denis Villeneuve"),
        changed_at=NOW - timedelta(days=LOOKBACK_DAYS + 1),
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.attachments_read, result.events_created) == (0, 0)
    assert await _events(session, film) == []


async def test_a_performer_a_trade_story_already_carded_is_not_carded_again(
    session, session_factory, run_id
):
    """`link` broke the casting a week before TMDB recorded the credit. Carding it here too
    would put the same attachment on the feed twice, the second time with no sources."""
    film = await add_film(session, 1)
    person = await _person(session, 1, "Zendaya")
    await _cast(session, film, person)
    session.add(
        Event(
            film_id=film.id,
            event_type="casting",
            confidence="confirmed",
            occurred_at=NOW - timedelta(days=6),
            subject_key=["zendaya"],
        )
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert (result.events_created, result.skipped) == (0, 1)
    assert len(await _events(session, film)) == 1


async def test_a_performer_carded_as_cast_still_cards_when_they_attach_to_direct(
    session, session_factory, run_id
):
    """Suppression is per beat, not per person. An actor-director carded when they joined the
    cast is not a card for their directing — and that is the beat this phase exists for."""
    film = await add_film(session, 1)
    person = await _person(session, 1, "Greta Gerwig")
    await _cast(session, film, person, changed_at=NOW - timedelta(days=3))
    await _attached(session, film, person)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 2
    assert [e.event_type for e in await _events(session, film)] == ["casting", "crew_attached"]


async def test_only_the_performers_already_carded_are_suppressed(session, session_factory, run_id):
    """Suppression is per person: a story that broke one casting must not swallow the two
    attachments arriving alongside it."""
    film = await add_film(session, 1)
    for person_id, name in ((1, "Zendaya"), (2, "Josh Brolin")):
        await _cast(session, film, await _person(session, person_id, name))
    session.add(
        Event(
            film_id=film.id,
            event_type="casting",
            confidence="confirmed",
            occurred_at=NOW - timedelta(days=6),
            subject_key=["zendaya"],
        )
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    carded = [e for e in await _events(session, film) if e.provenance == "catalog"]
    assert [e.subject_key for e in carded] == [["josh brolin"]]
    assert (await _summary(session, carded[0])).summary == "Josh Brolin joins the cast."


async def test_each_film_commits_on_its_own(session, session_factory, run_id, monkeypatch):
    """Commit per item: one film whose write blows up must not cost the others."""
    film = await add_film(session, 1)
    other = await add_film(session, 2)
    await _attached(session, film, await _person(session, 1, "Denis Villeneuve"))
    await _cast(session, other, await _person(session, 2, "Zendaya"))
    await session.commit()

    real = credit_events.write_deterministic_summary

    async def explode(session_, *, event_id, change, source_updated_at):
        if change.credits[0].role == "director":
            raise RuntimeError("summary write failed")
        return await real(
            session_, event_id=event_id, change=change, source_updated_at=source_updated_at
        )

    monkeypatch.setattr(credit_events, "write_deterministic_summary", explode)

    result = await _run(session_factory, run_id)

    assert (result.events_created, result.failures) == (1, 1)
    assert await _events(session, film) == []
    assert len(await _events(session, other)) == 1


async def test_consecutive_failures_abort_the_phase(session, session_factory, run_id, monkeypatch):
    film = await add_film(session, 1)
    for person_id, name in ((1, "A Person"), (2, "B Person")):
        await _cast(
            session,
            film,
            await _person(session, person_id, name),
            changed_at=NOW - timedelta(days=person_id),
        )
    await session.commit()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(credit_events, "write_deterministic_summary", explode)

    result = await _run(session_factory, run_id, failure_threshold=1)

    assert result.aborted is True
    assert result.abort_error == "aborted after 1 consecutive failures"
    assert result.failures == 1
    # The abort happens inside the exception handler, before events_created is incremented.
    assert result.events_created == 0


# ── Detachment carding tests (NEU-1200) ───────────────────────────────────


async def test_detachment_cards_when_prior_catalog_attachment(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person)
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    result = await _run_detachment(session_factory, run_id)

    assert result.events_created == 1
    assert result.detachments_read == 1
    events = await _events(session, film)
    removal = [e for e in events if e.event_type == CREDIT_REMOVED_EVENT_TYPE]
    assert len(removal) == 1
    assert removal[0].provenance == "catalog"
    assert removal[0].confidence == "rumored"
    assert removal[0].occurred_at == NOW
    assert removal[0].region is None
    assert removal[0].subject_key == ["denis villeneuve"]
    summary = await _summary(session, removal[0])
    assert summary.summary == "Denis Villeneuve is no longer attached to direct."
    assert summary.model == DETERMINISTIC_MODEL


async def test_detachment_cards_when_prior_story_attachment(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Timothée Chalamet")
    await session.commit()

    event = Event(
        film_id=film.id,
        event_type="casting",
        confidence="rumored",
        provenance="story",
        occurred_at=YESTERDAY,
        region=None,
        subject_key=["timothée chalamet"],
    )
    session.add(event)
    await session.flush()
    await session.commit()

    await _attached(
        session,
        film,
        person,
        credit_type="cast",
        job=None,
        change=CREDIT_REMOVED,
        changed_at=NOW,
    )
    await session.commit()

    result = await _run_detachment(session_factory, run_id)

    assert result.events_created == 1


async def test_detachment_skipped_when_no_prior_attachment(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Zendaya")
    await _attached(
        session,
        film,
        person,
        credit_type="cast",
        job=None,
        change=CREDIT_REMOVED,
        changed_at=NOW,
    )
    await session.commit()

    result = await _run_detachment(session_factory, run_id)

    assert result.events_created == 0


async def test_detachment_gate_requires_attachment_before_detachment(
    session, session_factory, run_id
):
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await session.commit()

    event = Event(
        film_id=film.id,
        event_type="crew_attached",
        confidence="rumored",
        provenance="catalog",
        occurred_at=NOW,
        region=None,
        subject_key=["denis villeneuve"],
    )
    session.add(event)
    await session.flush()
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=YESTERDAY)
    await session.commit()

    result = await _run_detachment(session_factory, run_id)

    assert result.events_created == 0


async def test_detachment_already_carded(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person)
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    result1 = await _run_detachment(session_factory, run_id)
    assert result1.events_created == 1

    result2 = await _run_detachment(session_factory, run_id)
    assert result2.events_created == 0
    assert result2.skipped >= 1


async def test_detachment_older_than_lookback(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person)
    await session.commit()
    await _run(session_factory, run_id)
    old = NOW - timedelta(days=LOOKBACK_DAYS + 1)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=old)
    await session.commit()

    result = await _run_detachment(session_factory, run_id)

    assert result.detachments_read == 0
    assert result.events_created == 0


async def test_detachment_one_card_per_observation(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Planned")
    director = await _person(session, 100, "Denis Villeneuve")
    actor = await _person(session, 200, "Timothée Chalamet")
    await _attached(session, film, director)
    await _cast(session, film, actor)
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, director, change=CREDIT_REMOVED, changed_at=NOW)
    await _cast(session, film, actor, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    result = await _run_detachment(session_factory, run_id)

    assert result.events_created == 1
    events = await _events(session, film)
    removal = [e for e in events if e.event_type == CREDIT_REMOVED_EVENT_TYPE]
    assert len(removal) == 1
    assert set(removal[0].subject_key or []) == {"denis villeneuve", "timothée chalamet"}
    summary = await _summary(session, removal[0])
    assert summary.summary == (
        "Denis Villeneuve is no longer attached to direct. Timothée Chalamet departs the cast."
    )


async def test_reattachment_after_removal_is_carded(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person, changed_at=YESTERDAY)
    await session.commit()
    result1 = await _run(session_factory, run_id)
    assert result1.events_created == 1
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()
    await _run_detachment(session_factory, run_id)
    later = NOW + timedelta(hours=1)
    await _attached(session, film, person, changed_at=later)
    await session.commit()

    result2 = await _run(session_factory, run_id, now=later + timedelta(hours=2))

    assert result2.events_created == 1


async def test_reattachment_without_removal_still_suppressed(session, session_factory, run_id):
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person, changed_at=YESTERDAY)
    await session.commit()
    result1 = await _run(session_factory, run_id)
    assert result1.events_created == 1
    await _attached(session, film, person, changed_at=NOW)
    await session.commit()

    result2 = await _run(session_factory, run_id)
    assert result2.events_created == 0
