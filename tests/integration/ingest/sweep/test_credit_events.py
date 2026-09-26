"""The sweep's credit-attachment phase end to end: which `catalog.film_credit_change` rows
become events, and what stops the same attachment becoming two cards.

The rule carrying the most weight is the one that is *not* implemented here at all — first
observation is a baseline (§5.3), which NEU-1082 guarantees upstream by writing no history
row for a film's first credit set. It is asserted anyway, because this is the phase where
getting it wrong would surface: tens of thousands of false "attached to direct" cards on the
first day the expansion ran.
"""

from datetime import UTC, datetime, timedelta

import httpx
import respx
from sqlalchemy import delete, select

from tests.fixtures.catalog import add_credit, add_film
from tests.fixtures.tmdb import make_person_details
from upmovies.catalog.models import FilmCredit, FilmCreditChange, Person
from upmovies.ingest import credit_holds
from upmovies.ingest.models import CreditHold
from upmovies.ingest.sweep import credit_events, run_credit_attachment_events
from upmovies.ingest.sweep.credit_events import (
    _card_detachment_group,
    run_credit_detachment_events,
)
from upmovies.ingest.tmdb.credit_history import (
    CREDIT_ADDED,
    CREDIT_REMOVED,
    RecordedCredit,
    diff_recorded_credits,
    record_credit_changes,
)
from upmovies.news.catalog_events import CREDIT_REMOVED_EVENT_TYPE
from upmovies.news.models import Event, EventSummary
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL, TEMPLATE_VERSION

NOW = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
LOOKBACK_DAYS = 7
QUARANTINE_HOURS = 72
# Old enough to have cleared a 72h hold, young enough to still be inside the 7-day window —
# the band every quarantine test that expects a card has to sit in.
AGED = NOW - timedelta(hours=QUARANTINE_HOURS + 1)
BASE_URL = "https://api.themoviedb.org/3"


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


async def _run_detachment(session_factory, run_id, *, dwell_days=0, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "run_id": run_id,
        "now": NOW,
        "lookback_days": LOOKBACK_DAYS,
        "dwell_days": dwell_days,
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


async def _credits(session, film):
    """The live cast/crew list — `catalog.film_credit` as the app would render it."""
    return (
        (
            await session.execute(
                select(FilmCredit).where(FilmCredit.film_id == film.id),
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
    """§5.3, the headline rule. Guaranteed upstream — `diff_recorded_credits` returns nothing for
    a film the catalog has never observed — so this drives the *real* writer rather than
    hand-inserting rows, which would assert nothing about production behaviour."""
    film = await add_film(session, 1)
    for person_id, name in ((1, "Denis Villeneuve"), (2, "Zendaya")):
        await _person(session, person_id, name)
    first_observation = diff_recorded_credits(
        previous=None,
        current=[
            RecordedCredit(person_id=1, credit_type="crew", job="Director"),
            RecordedCredit(person_id=2, credit_type="cast", job=None),
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


async def test_observations_clearing_in_one_pass_are_one_card(session, session_factory, run_id):
    """D-7 reverses NEU-1083's per-observation grouping. Quarantine releases a film's credits
    together however many observations they arrived over, so two performers three days apart
    that card in the same pass are one beat, dated at the later of them."""
    film = await add_film(session, 1)
    await _cast(
        session, film, await _person(session, 1, "Zendaya"), changed_at=NOW - timedelta(days=3)
    )
    await _cast(session, film, await _person(session, 2, "Josh Brolin"))
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.occurred_at == YESTERDAY
    assert event.subject_key == ["zendaya", "josh brolin"]


async def test_an_attachment_clearing_after_a_card_cards_only_itself(
    session, session_factory, run_id
):
    """The other half of collapsing: a burst that grows between passes must not re-card the
    people the first pass already named. The group's latest timestamp moves, so it is a new
    card under `uq_event_catalog_change`, and per-person suppression carries the rest."""
    film = await add_film(session, 1)
    early = await _person(session, 1, "Zendaya")
    await _cast(session, film, early, changed_at=NOW - timedelta(days=3))
    await session.commit()

    first = await _run(session_factory, run_id)

    await _cast(session, film, await _person(session, 2, "Josh Brolin"))
    await session.commit()

    second = await _run(session_factory, run_id)

    assert (first.events_created, second.events_created) == (1, 1)
    events = await _events(session, film)
    # Both are `casting`, so `_events`' ordering says nothing about which is which.
    assert {tuple(e.subject_key) for e in events} == {("zendaya",), ("josh brolin",)}


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


# ── Attachment quarantine (NEU-1368, ADR-0017 D-3) ─────────────────────────


async def _still_attached(
    session, film, person, *, credit_type="crew", job: str | None = "Director", credit_order=None
):
    """Put the credit in `catalog.film_credit` — the live-state half of the gate. A test that
    omits this is asserting the *reverted* case, not merely leaving setup out.

    `credit_order` is load-bearing for a cast row and meaningless for a crew one: seed grade
    for cast *is* the top-5 billing, so a cast credit written without one is not present as
    far as this gate is concerned.
    """
    await add_credit(
        session,
        film,
        person.id,
        credit_type=credit_type,
        job=job,
        credit_order=credit_order,
    )


async def _quarantined(session_factory, run_id, **overrides):
    return await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, **overrides)


async def test_an_attachment_inside_the_window_is_held_rather_than_carded(
    session, session_factory, run_id
):
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person)
    await _still_attached(session, film, person)
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert await _events(session, film) == []
    # Read, not carded, not skipped: the row is still in play and will be re-judged next pass.
    assert (result.attachments_read, result.held) == (1, 1)
    assert (result.events_created, result.skipped) == (0, 0)


async def test_an_attachment_reverted_inside_the_window_never_cards(
    session, session_factory, run_id
):
    """D-3's whole point, and the project-level acceptance clause: added and reverted inside
    the window produces zero events *and* a correct live cast list throughout. The credit row
    is deliberately absent — that is what the revert looks like once the next ingest rebuilds
    `film_credit` — and the attachment has aged past the hold, so only presence is stopping
    it."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person, changed_at=AGED)
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert await _events(session, film) == []
    assert (result.attachments_read, result.held, result.events_created) == (1, 1, 0)
    # The state half is untouched: quarantine holds *events*, never the live cast list.
    assert await _credits(session, film) == []


async def test_an_attachment_that_survives_the_window_cards_once(session, session_factory, run_id):
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert (result.events_created, result.held) == (1, 0)
    (event,) = await _events(session, film)
    # The beat keeps the time it *happened*, not the time the hold released it — the
    # publication axis is `created_at` (ADR-0016), and this is the other one.
    assert event.occurred_at == AGED
    assert event.subject_key == ["denis villeneuve"]

    # And only once: the rolling window re-reads it on the next pass.
    again = await _quarantined(session_factory, run_id)
    assert (again.events_created, again.skipped, again.held) == (0, 1, 0)
    assert len(await _events(session, film)) == 1


async def test_the_hold_releases_the_pass_the_window_closes(session, session_factory, run_id):
    """Exactly at the boundary, not the pass after it: the gate is `changed_at + N <= now`."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person, changed_at=NOW - timedelta(hours=QUARANTINE_HOURS))
    await _still_attached(session, film, person)
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert result.events_created == 1


async def test_a_credit_present_in_another_role_is_still_held(session, session_factory, run_id):
    """Presence is scoped to the seed-grade role the attachment recorded. A performer whose
    directing credit was reverted is still in the cast, and the cast row must not release the
    directing beat — the mirror of the removal gate's role scoping (NEU-1205)."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Greta Gerwig")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person, credit_type="cast", job=None, credit_order=0)
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert (result.events_created, result.held) == (0, 1)


async def test_a_cast_credit_below_the_seed_grade_does_not_release_the_hold(
    session, session_factory, run_id
):
    """`film_credit` is delete-and-rebuilt, so a performer who has slipped out of the top-5
    billing holds a row that is no longer seed grade — which is exactly how the credit-history
    diff would read them, as removed. The gate reads the same predicate."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Zendaya")
    await _cast(session, film, person, changed_at=AGED)
    await add_credit(session, film, person.id, credit_type="cast", job=None, credit_order=9)
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert (result.events_created, result.held) == (0, 1)


async def test_the_gate_holds_people_individually_within_one_observation(
    session, session_factory, run_id
):
    """One reverted name must not take the rest of its group down with it — the same
    discipline removal-aware suppression already applies per person."""
    film = await add_film(session, 1)
    stayed = await _person(session, 1, "Zendaya")
    reverted = await _person(session, 2, "Rebecca Ferguson")
    await _cast(session, film, stayed, changed_at=AGED)
    await _cast(session, film, reverted, changed_at=AGED)
    await _still_attached(session, film, stayed, credit_type="cast", job=None, credit_order=0)
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert (result.attachments_read, result.held, result.events_created) == (2, 1, 1)
    (event,) = await _events(session, film)
    assert event.subject_key == ["zendaya"]


async def test_a_credit_released_after_its_burst_carded_still_gets_its_card(
    session, session_factory, run_id
):
    """The card is dated by whom it *names*, not by the whole group. A credit the presence
    check held while the rest of its burst carded is released on a later pass into a group
    whose latest `changed_at` is already taken — dating it by the group would find that card
    and silently drop the one person still owed one."""
    film = await add_film(session, 1)
    held = await _person(session, 1, "Rebecca Ferguson")
    carded = await _person(session, 2, "Zendaya")
    await _cast(session, film, held, changed_at=NOW - timedelta(days=5))
    await _cast(session, film, carded, changed_at=NOW - timedelta(days=4))
    # Only Zendaya is live, so Rebecca is held on presence while Zendaya cards.
    await _still_attached(session, film, carded, credit_type="cast", job=None, credit_order=0)
    await session.commit()

    first = await _quarantined(session_factory, run_id)

    assert (first.events_created, first.held) == (1, 1)

    # The next ingest restores her credit; the gate now releases the older attachment into a
    # group whose latest change is the one already carded.
    await _still_attached(session, film, held, credit_type="cast", job=None, credit_order=1)
    await session.commit()

    second = await _quarantined(session_factory, run_id)

    assert second.events_created == 1
    events = await _events(session, film)
    assert {tuple(e.subject_key) for e in events} == {("zendaya",), ("rebecca ferguson",)}
    # Dated at her own change, not at the group's latest — which Zendaya's card holds.
    (hers,) = [e for e in events if e.subject_key == ["rebecca ferguson"]]
    assert hers.occurred_at == NOW - timedelta(days=5)


async def test_a_card_is_dated_by_the_people_it_names(session, session_factory, run_id):
    """A burst whose latest member a trade story already carded dates its card at the latest
    change it actually names — `occurred_at` is the beat's time, and the beat is the people
    on the card."""
    film = await add_film(session, 1)
    fresh = await _person(session, 1, "Rebecca Ferguson")
    already = await _person(session, 2, "Zendaya")
    await _cast(session, film, fresh, changed_at=NOW - timedelta(days=5))
    await _cast(session, film, already, changed_at=NOW - timedelta(days=4))
    for person in (fresh, already):
        await _still_attached(session, film, person, credit_type="cast", job=None, credit_order=0)
    # A trade story carded Zendaya first — the Tier-A case `_uncarded_attachments` exists for.
    session.add(
        Event(
            film_id=film.id,
            event_type="casting",
            confidence="confirmed",
            provenance="story",
            occurred_at=NOW - timedelta(days=6),
            subject_key=["zendaya"],
        )
    )
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert result.events_created == 1
    (catalog_event,) = [e for e in await _events(session, film) if e.provenance == "catalog"]
    assert catalog_event.subject_key == ["rebecca ferguson"]
    assert catalog_event.occurred_at == NOW - timedelta(days=5)


async def test_a_burst_clearing_together_is_one_card_in_billing_order(
    session, session_factory, run_id
):
    """The done-when clause. A whole top-billed cast observed over four days, every hold
    expiring by this pass: one `casting` card naming all of them, dated at the latest of
    them, with the body reading in TMDB's billing order rather than in the order the history
    diff emitted.

    Five people, not the ticket's six: `TOP_BILLED_ORDER` is 5, so a sixth cast credit is
    below the seed grade — the history never records it and the gate's presence check would
    never release it. Five *is* the whole burst.
    """
    film = await add_film(session, 1)
    # Deliberately mismatched: the diff order (by `changed_at`) is the reverse of billing.
    names = ["Fifth", "Fourth", "Third", "Second", "First"]
    days = [7, 7, 6, 5, 4]
    for offset, (name, day) in enumerate(zip(names, days, strict=True)):
        person = await _person(session, 100 + offset, name)
        await _cast(session, film, person, changed_at=NOW - timedelta(days=day))
        await _still_attached(
            session, film, person, credit_type="cast", job=None, credit_order=4 - offset
        )
    await session.commit()

    result = await _quarantined(session_factory, run_id)

    assert (result.attachments_read, result.events_created, result.held) == (5, 1, 0)
    (event,) = await _events(session, film)
    assert event.event_type == "casting"
    assert event.occurred_at == NOW - timedelta(days=4)
    assert event.subject_key == ["first", "second", "third", "fourth", "fifth"]
    assert (await _summary(session, event)).summary == (
        "First, Second, Third, Fourth and Fifth join the cast."
    )

    # And the re-read stays idempotent: same rows, same latest timestamp, same card.
    again = await _quarantined(session_factory, run_id)
    assert (again.events_created, again.skipped) == (0, 1)


async def test_quarantine_zero_cards_immediately(session, session_factory, run_id):
    """0 disables *both* conditions, reverting to pre-D-3 behaviour — note there is no
    `film_credit` row here at all, and it cards anyway."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=0)

    assert (result.events_created, result.held) == (1, 0)


# ── Sanity holds (D-8, NEU-1370) ──────────────────────────────────────────


SANITY = {"max_films_per_day": 20, "posthumous_years": 2, "min_age_years": 3}
"""The shipped thresholds, passed explicitly: every test above runs with the checks off, which
is the pre-D-8 behaviour the phase still has to have."""


async def _sane(session_factory, run_id, **overrides):
    """A pass with quarantine *and* the sanity checks on — how production runs it."""
    return await _run(
        session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, **{**SANITY, **overrides}
    )


def _person_payload(person_id: int, name: str, **overrides):
    return make_person_details(person_id, name=name, profile_path="/p.jpg", **overrides)


def _mock_person(person_id: int, name: str, **overrides):
    return respx.get(f"{BASE_URL}/person/{person_id}").mock(
        return_value=httpx.Response(200, json=_person_payload(person_id, name, **overrides))
    )


async def _holds(session, **filters):
    stmt = select(CreditHold).order_by(CreditHold.changed_at, CreditHold.film_id)
    for column, value in filters.items():
        stmt = stmt.where(getattr(CreditHold, column) == value)
    return (
        (await session.execute(stmt, execution_options={"populate_existing": True})).scalars().all()
    )


async def _burst(session, person, *, films: int, changed_at=AGED, attached: int | None = None):
    """`films` attachments of one person on one day, the first `attached` of them still in
    `catalog.film_credit`. Defaults to all of them still attached."""
    made = []
    for n in range(films):
        film = await add_film(session, 100 + n)
        await _attached(session, film, person, changed_at=changed_at)
        if attached is None or n < attached:
            await _still_attached(session, film, person)
        made.append(film)
    return made


async def test_a_vandalism_burst_holds_every_row_with_a_reason(session, session_factory, run_id):
    """The acceptance case: one person attached to 25 films in a day cards nothing, and every
    withheld row says why."""
    person = await _person(session, 100, "Vandalised Person")
    await _burst(session, person, films=25)
    await session.commit()

    result = await _sane(session_factory, run_id)

    assert result.events_created == 0
    assert result.holds_new == 25
    holds = await _holds(session)
    assert len(holds) == 25
    assert {h.reason for h in holds} == {"burst"}
    assert all(h.released_at is None and h.release_reason is None for h in holds)


async def test_an_ordinary_attachment_is_untouched_by_the_checks(session, session_factory, run_id):
    """The other half of the acceptance clause: a normal attachment still cards, and writes no
    hold row at all."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()

    result = await _sane(session_factory, run_id)

    assert (result.events_created, result.holds_new) == (1, 0)
    assert await _holds(session) == []


async def test_a_burst_under_the_threshold_cards(session, session_factory, run_id):
    """Nineteen against a threshold of twenty — the check is `>=`, and one below it is a
    prolific day rather than an attack."""
    person = await _person(session, 100, "Prolific Person")
    await _burst(session, person, films=19)
    await session.commit()

    result = await _sane(session_factory, run_id)

    assert result.holds_new == 0
    assert result.events_created == 19


async def test_a_burst_spread_over_two_days_is_two_days(session, session_factory, run_id):
    """The bucket is the UTC observation day: thirteen and twelve clear the bar separately,
    and neither reaches it."""
    person = await _person(session, 100, "Busy Person")
    await _burst(session, person, films=13, changed_at=AGED)
    for n in range(12):
        film = await add_film(session, 200 + n)
        await _attached(session, film, person, changed_at=AGED - timedelta(days=1))
        await _still_attached(session, film, person)
    await session.commit()

    result = await _sane(session_factory, run_id)

    assert result.holds_new == 0
    assert result.events_created == 25


async def test_the_burst_clears_when_tmdb_reverts_it_and_the_survivors_card(
    session, session_factory, run_id
):
    """The full acceptance path. 25 held; TMDB then drops 20 of them from `film_credit`; the
    next pass clears every hold and cards the five that are still real."""
    person = await _person(session, 100, "Vandalised Person")
    films = await _burst(session, person, films=25)
    await session.commit()

    first = await _sane(session_factory, run_id)
    assert (first.holds_new, first.events_created) == (25, 0)

    # TMDB reverts twenty of them: `film_credit` is delete-and-rebuilt, so they simply go.
    await session.execute(
        delete(FilmCredit).where(FilmCredit.film_id.in_([f.id for f in films[:20]]))
    )
    await session.commit()

    second = await _sane(session_factory, run_id)

    assert second.holds_cleared == 25
    assert second.events_created == 5
    # The twenty reverted ones are back in the backlog and stopped by quarantine's presence
    # condition, which is where a change that was never true belongs.
    assert second.held == 20
    assert {h.release_reason for h in await _holds(session)} == {"cleared"}


async def test_a_partly_reverted_burst_is_still_held_on_the_pass_it_is_seen(
    session, session_factory, run_id
):
    """Detection reads the *recorded* count, not the live one. Eight of the twenty-five are
    already gone by the time quarantine releases the rest, and the seventeen survivors must not
    walk past a check whose whole subject is the run they came from."""
    person = await _person(session, 100, "Vandalised Person")
    await _burst(session, person, films=25, attached=17)
    await session.commit()

    result = await _sane(session_factory, run_id)

    assert result.events_created == 0
    # Seventeen: the eight quarantine already dropped as reverted never reach the check.
    assert (result.holds_new, result.held) == (17, 8)
    assert {h.reason for h in await _holds(session)} == {"burst"}


async def test_a_cleared_hold_is_never_reheld(session, session_factory, run_id):
    """The seam between the two counts. Clearing reads the live count and detection reads the
    recorded one, which is append-only — so without this the survivors of a reverted burst
    would be released and re-held on every pass forever, and §3's "the survivors then card
    normally" would never happen."""
    person = await _person(session, 100, "Vandalised Person")
    await _burst(session, person, films=25, attached=17)
    await session.commit()
    await _sane(session_factory, run_id)

    second = await _sane(session_factory, run_id)

    assert second.holds_cleared == 17
    assert second.holds_new == 0
    assert second.events_created == 17
    third = await _sane(session_factory, run_id)
    assert (third.holds_new, third.holds_cleared) == (0, 0)


async def test_turning_the_burst_check_off_releases_what_it_is_holding(
    session, session_factory, run_id
):
    """A threshold of 0 is the check switched off, and a switched-off check cannot go on
    withholding on a condition nothing will ever evaluate again."""
    person = await _person(session, 100, "Vandalised Person")
    await _burst(session, person, films=25)
    await session.commit()
    await _sane(session_factory, run_id)

    result = await _sane(session_factory, run_id, max_films_per_day=0)

    assert result.holds_cleared == 25
    assert result.events_created == 25


async def test_a_held_attachment_is_not_carded_while_the_hold_is_open(
    session, session_factory, run_id
):
    """The hold has to keep the row out of the *backlog*, not merely out of one group: a second
    pass over the same window must not card it either."""
    person = await _person(session, 100, "Vandalised Person")
    await _burst(session, person, films=25)
    await session.commit()

    await _sane(session_factory, run_id)
    second = await _sane(session_factory, run_id)

    assert (second.attachments_read, second.events_created) == (0, 0)
    # Re-held rather than re-written: the grain is the observation.
    assert (second.holds_new, second.holds_cleared) == (0, 0)
    assert len(await _holds(session)) == 25


async def test_a_hold_expires_when_its_change_leaves_the_window(session, session_factory, run_id):
    """`deceased` and `implausible_age` never clear, so the window is what ends them — and an
    expired hold never cards, because its change is no longer read."""
    person = await _person(session, 100, "Vandalised Person")
    await _burst(session, person, films=25)
    await session.commit()

    await _sane(session_factory, run_id)

    later = NOW + timedelta(days=LOOKBACK_DAYS)
    result = await _sane(session_factory, run_id, now=later)

    assert result.holds_expired == 25
    assert result.events_created == 0
    assert {h.release_reason for h in await _holds(session)} == {"expired"}


@respx.mock
async def test_a_credit_long_after_a_death_is_held(session, session_factory, run_id, tmdb_client):
    film = await add_film(session, 1)
    person = await _person(session, 100, "Long Departed")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()
    _mock_person(100, "Long Departed", deathday="2015-01-01")

    result = await _sane(session_factory, run_id, client=tmdb_client)

    assert (result.events_created, result.holds_new) == (0, 1)
    (hold,) = await _holds(session)
    assert hold.reason == "deceased"


@respx.mock
async def test_a_posthumous_credit_inside_the_window_cards(
    session, session_factory, run_id, tmdb_client
):
    """A film completed before the death, or archive footage, is an ordinary beat — the check
    is for credits arriving *long* after."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Recently Departed")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()
    _mock_person(100, "Recently Departed", deathday=str((AGED - timedelta(days=365)).date()))

    result = await _sane(session_factory, run_id, client=tmdb_client)

    assert (result.events_created, result.holds_new) == (1, 0)


@respx.mock
async def test_an_infant_billed_as_a_lead_is_held(session, session_factory, run_id, tmdb_client):
    film = await add_film(session, 1)
    person = await _person(session, 100, "One Year Old")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()
    _mock_person(100, "One Year Old", birthday=str((AGED - timedelta(days=365)).date()))

    result = await _sane(session_factory, run_id, client=tmdb_client)

    assert (result.events_created, result.holds_new) == (0, 1)
    (hold,) = await _holds(session)
    assert hold.reason == "implausible_age"


@respx.mock
async def test_a_child_actor_old_enough_still_cards(session, session_factory, run_id, tmdb_client):
    """The bar is 3, not "a child": twelve-year-olds are cast, and holding them would be the
    check costing more than the vandalism."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Twelve Year Old")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()
    _mock_person(100, "Twelve Year Old", birthday=str((AGED - timedelta(days=365 * 12)).date()))

    result = await _sane(session_factory, run_id, client=tmdb_client)

    assert (result.events_created, result.holds_new) == (1, 0)


@respx.mock
async def test_person_details_are_fetched_once_and_only_for_people_about_to_card(
    session, session_factory, run_id, tmdb_client
):
    """The cost clause. One request for the person who reaches the checks; none at all for the
    one quarantine already withheld; and none again on the second pass."""
    carding = await add_film(session, 1)
    reverted = await add_film(session, 2)
    carded_person = await _person(session, 100, "Carded Person")
    held_person = await _person(session, 200, "Reverted Person")
    await _attached(session, carding, carded_person, changed_at=AGED)
    await _still_attached(session, carding, carded_person)
    # No `film_credit` row: quarantine drops this one before the sanity checks ever see it.
    await _attached(session, reverted, held_person, changed_at=AGED)
    await session.commit()
    carded_route = _mock_person(100, "Carded Person")
    held_route = _mock_person(200, "Reverted Person")

    await _sane(session_factory, run_id, client=tmdb_client)
    await _sane(session_factory, run_id, client=tmdb_client)

    assert carded_route.call_count == 1
    assert held_route.call_count == 0
    person = await session.get(Person, 100, populate_existing=True)
    assert person is not None and person.details_observed_at is not None


@respx.mock
async def test_a_person_tmdb_has_deleted_is_tombstoned_and_not_held(
    session, session_factory, run_id, tmdb_client
):
    """A 404 is terminal, not an outage: there are no dates, so there is nothing to hold on."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Deleted Person")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()
    respx.get(f"{BASE_URL}/person/100").mock(return_value=httpx.Response(404, json={}))

    result = await _sane(session_factory, run_id, client=tmdb_client)

    assert (result.events_created, result.holds_new) == (1, 0)
    refreshed = await session.get(Person, 100, populate_existing=True)
    assert refreshed is not None and refreshed.tmdb_missing_at is not None


@respx.mock
async def test_a_deleted_person_is_asked_about_once_and_not_once_a_pass(
    session, session_factory, run_id, tmdb_client
):
    """A 404 is an answer, and it stamps `details_observed_at` like any other. Without that a
    dead person id in the rolling window is re-requested on every pass for a week."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Deleted Person")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()
    route = respx.get(f"{BASE_URL}/person/100").mock(return_value=httpx.Response(404, json={}))

    await _sane(session_factory, run_id, client=tmdb_client)
    await _sane(session_factory, run_id, client=tmdb_client)

    assert route.call_count == 1


@respx.mock
async def test_a_tmdb_outage_costs_the_holds_and_not_the_pass(
    session, session_factory, run_id, tmdb_client
):
    """The date checks are a filter over what would otherwise card, so failing to reach TMDB
    must cost only the holds they would have placed."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Unreachable Person")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()
    respx.get(f"{BASE_URL}/person/100").mock(side_effect=httpx.ConnectError("no route"))

    result = await _sane(session_factory, run_id, client=tmdb_client)

    assert (result.events_created, result.holds_new) == (1, 0)
    # Nothing stamped, so the next pass asks again rather than trusting an absent date.
    refreshed = await session.get(Person, 100, populate_existing=True)
    assert refreshed is not None and refreshed.details_observed_at is None


async def test_a_manually_released_hold_cards_on_the_next_pass(session, session_factory, run_id):
    person = await _person(session, 100, "Vandalised Person")
    films = await _burst(session, person, films=25)
    await session.commit()
    await _sane(session_factory, run_id)

    released = next(h for h in await _holds(session) if h.film_id == films[0].id)
    await credit_holds.release_hold(session, hold_id=released.id)
    await session.commit()

    result = await _sane(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, films[0])
    assert event.event_type == "crew_attached"
    # And it stays released: the check would otherwise re-hold it on every pass forever.
    assert result.holds_new == 0
    again = await _sane(session_factory, run_id)
    assert again.holds_new == 0


async def test_an_expired_hold_never_cards(session, session_factory, run_id):
    person = await _person(session, 100, "Vandalised Person")
    films = await _burst(session, person, films=25)
    await session.commit()
    await _sane(session_factory, run_id)

    later = NOW + timedelta(days=LOOKBACK_DAYS)
    await _sane(session_factory, run_id, now=later)
    result = await _sane(session_factory, run_id, now=later)

    assert result.events_created == 0
    assert await _events(session, films[0]) == []


async def test_a_hold_on_one_role_leaves_the_other_alone(session, session_factory, run_id):
    """The hold is keyed on the role, so an actor-director held for one credit still cards the
    other — which is why the backlog filters in Python rather than anti-joining on time."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Actor Director")
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await _cast(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person, credit_type="cast", job=None, credit_order=0)
    await session.commit()
    # Hold only the directing half, by hand, and on a reason that does not clear: a `burst`
    # row would be released by `reconcile_holds` on this very pass, since one film is nowhere
    # near the threshold.
    session.add(
        CreditHold(
            film_id=film.id,
            person_id=person.id,
            credit_type="director",
            changed_at=AGED,
            reason="deceased",
            held_at=NOW,
        )
    )
    await session.commit()

    result = await _sane(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.event_type == "casting"


async def test_the_checks_are_off_by_default(session, session_factory, run_id):
    """Every threshold defaults to 0 and `client` to None, so a caller without a `Settings`
    gets the pre-D-8 behaviour — 25 films in a day, all carded."""
    person = await _person(session, 100, "Vandalised Person")
    await _burst(session, person, films=25)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.holds_new == 0
    assert result.events_created == 25


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


# ── Forward-dwell gate tests (NEU-1205) ───────────────────────────────────────

DWELL_DAYS = 3


async def test_flap_suppressed_when_reattach_within_window(session, session_factory, run_id):
    """A removal followed by a re-attachment within N days is a flap and is suppressed."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = NOW - timedelta(days=DWELL_DAYS)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(session, film, person, changed_at=removed_at + timedelta(days=DWELL_DAYS - 1))
    await session.commit()

    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)

    assert result.events_created == 0
    assert len(await _events(session, film)) == 1  # only the original attachment


async def test_final_departure_carded_when_no_reattach(session, session_factory, run_id):
    """A removal with no re-attachment within N days cards normally."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = NOW - timedelta(days=DWELL_DAYS)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await session.commit()

    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)

    assert result.events_created == 1
    removal = [e for e in await _events(session, film) if e.event_type == CREDIT_REMOVED_EVENT_TYPE]
    assert len(removal) == 1


async def test_held_removal_not_carded_within_window(session, session_factory, run_id):
    """A removal younger than N days is held, not carded, and not counted as a failure."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = NOW - timedelta(days=DWELL_DAYS - 1)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await session.commit()

    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)

    assert result.events_created == 0
    assert result.failures == 0


async def test_held_removal_cards_after_window_passes(session, session_factory, run_id):
    """A held removal cards once the hold passes and no re-attachment is observed."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = NOW - timedelta(days=1)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await session.commit()

    held = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)
    assert held.events_created == 0

    later = NOW + timedelta(days=DWELL_DAYS)
    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS, now=later)

    assert result.events_created == 1
    removal = [e for e in await _events(session, film) if e.event_type == CREDIT_REMOVED_EVENT_TYPE]
    assert len(removal) == 1
    assert removal[0].occurred_at == removed_at


async def test_flap_then_final_departure_cards_only_final(session, session_factory, run_id):
    """Maya Boyd sequence: add/remove/add/remove collapses to attach + final remove."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    # Timeline: added 8/24, removed 8/25, added 8/27, removed 8/28; now is 8/29.
    base = datetime(2026, 8, 24, tzinfo=UTC)
    await _cast(session, film, person, changed_at=base)
    await _cast(session, film, person, change=CREDIT_REMOVED, changed_at=base + timedelta(days=1))
    await _cast(session, film, person, changed_at=base + timedelta(days=3))
    await _cast(session, film, person, change=CREDIT_REMOVED, changed_at=base + timedelta(days=4))
    await session.commit()

    now = datetime(2026, 8, 29, tzinfo=UTC)
    await _run(session_factory, run_id, now=now)
    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS, now=now)

    # The 8/25 removal is held; the 8/27 re-attachment is suppressed by removal-aware
    # suppression (the 8/25 removal is held and invisible). The 8/28 removal is held.
    assert result.events_created == 0
    events = await _events(session, film)
    assert [e.event_type for e in events] == ["casting"]

    # Advance past the 8/28 removal's hold: both the 8/25 flap and 8/27 re-attachment are
    # now fully observed, so only the 8/28 final removal cards.
    later = now + timedelta(days=DWELL_DAYS)
    result2 = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS, now=later)

    assert result2.events_created == 1
    events = await _events(session, film)
    removal = [e for e in events if e.event_type == CREDIT_REMOVED_EVENT_TYPE]
    assert len(removal) == 1
    assert removal[0].occurred_at == base + timedelta(days=4)


async def test_per_person_gate_in_group(session, session_factory, run_id):
    """A mixed group cards only the final departures, dropping the flap person."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    final = await _person(session, 100, "Final Departer")
    flap = await _person(session, 200, "Flap Person")
    removed_at = NOW - timedelta(days=DWELL_DAYS)
    await _attached(session, film, final, changed_at=removed_at - timedelta(days=1))
    await _attached(session, film, flap, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, final, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(session, film, flap, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(session, film, flap, changed_at=removed_at + timedelta(days=1))
    await session.commit()

    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)

    assert result.events_created == 1
    removal = [e for e in await _events(session, film) if e.event_type == CREDIT_REMOVED_EVENT_TYPE]
    assert removal[0].subject_key == ["final departer"]


async def test_forward_gate_reads_raw_history_not_events(session, session_factory, run_id):
    """The flap's re-attachment is suppressed (never carded), but the gate still sees it."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = NOW - timedelta(days=DWELL_DAYS)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(session, film, person, changed_at=removed_at + timedelta(days=DWELL_DAYS - 1))
    await session.commit()

    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)

    assert result.events_created == 0
    # The re-attachment never carded, proving the gate read raw history.
    assert [e.event_type for e in await _events(session, film)] == ["crew_attached"]


async def test_forward_gate_role_scoped(session, session_factory, run_id):
    """A cast removal followed by a director arrival within N days is not a flap."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Greta Gerwig")
    removed_at = NOW - timedelta(days=DWELL_DAYS)
    await _cast(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _cast(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(
        session,
        film,
        person,
        credit_type="crew",
        job="Director",
        changed_at=removed_at + timedelta(days=1),
    )
    await session.commit()
    await _run(session_factory, run_id)

    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)

    # Cast removal cards because the director arrival is a different role.
    assert result.events_created == 1
    events = await _events(session, film)
    assert {e.event_type for e in events} == {"casting", "crew_attached", CREDIT_REMOVED_EVENT_TYPE}


async def test_dwell_zero_disables_gate(session, session_factory, run_id):
    """dwell_days=0 reverts to plain NEU-1200: a flap cards its removal."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = NOW - timedelta(days=1)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(session, film, person, changed_at=removed_at + timedelta(hours=1))
    await session.commit()

    result = await _run_detachment(session_factory, run_id, dwell_days=0)

    assert result.events_created == 1


async def test_determinism_reread_stable(session, session_factory, run_id):
    """A carded final removal is skipped on re-read; a held removal stays held."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = NOW - timedelta(days=DWELL_DAYS)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await session.commit()

    first = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)
    assert first.events_created == 1

    second = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)
    assert second.events_created == 0
    assert second.skipped >= 1

    held_at = NOW - timedelta(days=1)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=held_at)
    await session.commit()

    third = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)
    assert third.events_created == 0


async def test_prior_attachment_gate_still_applies(session, session_factory, run_id):
    """A baseline credit (never carded) removed and aged past N days still emits no card."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Zendaya")
    removed_at = NOW - timedelta(days=DWELL_DAYS)
    await _attached(
        session,
        film,
        person,
        credit_type="cast",
        job=None,
        change=CREDIT_REMOVED,
        changed_at=removed_at,
    )
    await session.commit()

    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)

    assert result.events_created == 0


async def test_forward_window_half_open(session, session_factory, run_id):
    """A re-attachment exactly at changed_at + N is outside the window."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = NOW - timedelta(days=DWELL_DAYS)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(session, film, person, changed_at=removed_at + timedelta(days=DWELL_DAYS))
    await session.commit()

    result = await _run_detachment(session_factory, run_id, dwell_days=DWELL_DAYS)

    assert result.events_created == 1


# ── Backfill behaviour (NEU-1205) ──────────────────────────────────────────


async def test_backfill_applies_forward_gate(session):
    """Backlog removals: the hold trivially passes, only the forward gate applies."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    final = await _person(session, 100, "Final Departer")
    flap = await _person(session, 200, "Flap Person")
    removed_at = datetime(2020, 1, 1, tzinfo=UTC)
    await _attached(session, film, final, changed_at=removed_at - timedelta(days=1))
    await _attached(session, film, flap, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    # Card the original attachments so the prior-attachment gate passes.
    session.add(
        Event(
            film_id=film.id,
            event_type="casting",
            confidence="rumored",
            provenance="catalog",
            occurred_at=removed_at - timedelta(days=1),
            region=None,
            subject_key=["final departer", "flap person"],
        )
    )
    await session.flush()
    await _attached(session, film, final, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(session, film, flap, change=CREDIT_REMOVED, changed_at=removed_at)
    await _attached(session, film, flap, changed_at=removed_at + timedelta(days=1))
    await session.commit()

    from upmovies.ingest.sweep.credit_events import group_detachments, load_detachment_backlog

    detachments = await load_detachment_backlog(session)
    (group,) = group_detachments(detachments)
    carded = await _card_detachment_group(
        session,
        group=group,
        now=datetime(2025, 1, 1, tzinfo=UTC),
        dwell_days=DWELL_DAYS,
    )
    await session.commit()

    assert carded is True
    removal = [e for e in await _events(session, film) if e.event_type == CREDIT_REMOVED_EVENT_TYPE]
    assert len(removal) == 1
    assert removal[0].subject_key == ["final departer"]


async def test_backfill_skips_already_carded(session):
    """Forward-only: an already-carded removal is left in place, not destructively cleaned."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Maya Boyd")
    removed_at = datetime(2020, 1, 1, tzinfo=UTC)
    await _attached(session, film, person, changed_at=removed_at - timedelta(days=1))
    await session.commit()
    async with session.begin_nested():
        session.add(
            Event(
                film_id=film.id,
                event_type=CREDIT_REMOVED_EVENT_TYPE,
                confidence="rumored",
                provenance="catalog",
                occurred_at=removed_at,
                region=None,
                subject_key=["maya boyd"],
            )
        )
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=removed_at)
    await session.commit()

    from upmovies.ingest.sweep.credit_events import group_detachments, load_detachment_backlog

    detachments = await load_detachment_backlog(session)
    (group,) = group_detachments(detachments)
    carded = await _card_detachment_group(
        session,
        group=group,
        now=datetime(2025, 1, 1, tzinfo=UTC),
        dwell_days=DWELL_DAYS,
    )

    assert carded is False


# ── Supersession write (NEU-1347, ADR-0017 D-2) ────────────────────────────


async def _by_type(session, film, event_type):
    return [e for e in await _events(session, film) if e.event_type == event_type]


async def test_a_removal_supersedes_the_prior_attachment_card(session, session_factory, run_id):
    """Attach → remove: the attachment card is marked, linked to the removal, and still there."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person)
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    await _run_detachment(session_factory, run_id)

    (attachment,) = await _by_type(session, film, "crew_attached")
    (removal,) = await _by_type(session, film, CREDIT_REMOVED_EVENT_TYPE)
    assert attachment.status == "superseded"
    assert attachment.superseded_by == removal.id
    assert removal.status == "published"
    assert removal.superseded_by is None


async def test_a_reattachment_after_a_removal_is_a_fresh_published_card(
    session, session_factory, run_id
):
    """Attach → remove → re-attach yields superseded, published, published."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person, changed_at=YESTERDAY)
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()
    await _run_detachment(session_factory, run_id)
    later = NOW + timedelta(hours=1)
    await _attached(session, film, person, changed_at=later)
    await session.commit()
    await _run(session_factory, run_id, now=later + timedelta(hours=2))

    events = sorted(await _events(session, film), key=lambda e: e.occurred_at)
    (removal,) = await _by_type(session, film, CREDIT_REMOVED_EVENT_TYPE)
    assert [(e.event_type, e.status) for e in events] == [
        ("crew_attached", "superseded"),
        (CREDIT_REMOVED_EVENT_TYPE, "published"),
        ("crew_attached", "published"),
    ]
    assert [e.superseded_by for e in events] == [removal.id, None, None]


async def test_a_removal_supersedes_a_story_attachment_card(session, session_factory, run_id):
    """Any provenance: a trade-story casting card is superseded the same as a catalog one."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Timothée Chalamet")
    story_card = Event(
        film_id=film.id,
        event_type="casting",
        confidence="rumored",
        provenance="story",
        occurred_at=YESTERDAY,
        region=None,
        subject_key=["timothée chalamet"],
    )
    session.add(story_card)
    await session.flush()
    await _cast(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    await _run_detachment(session_factory, run_id)

    (casting,) = await _by_type(session, film, "casting")
    (removal,) = await _by_type(session, film, CREDIT_REMOVED_EVENT_TYPE)
    assert casting.id == story_card.id
    assert casting.status == "superseded"
    assert casting.superseded_by == removal.id


async def test_a_removal_supersedes_only_the_most_recent_attachment_card(
    session, session_factory, run_id
):
    """Two attachment cards before the removal: the most recent one is marked, not both."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Greta Gerwig")
    older = Event(
        film_id=film.id,
        event_type="casting",
        confidence="rumored",
        provenance="story",
        occurred_at=YESTERDAY - timedelta(days=3),
        region=None,
        subject_key=["greta gerwig"],
    )
    session.add(older)
    await session.flush()
    await _attached(session, film, person, changed_at=YESTERDAY)
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    await _run_detachment(session_factory, run_id)

    (casting,) = await _by_type(session, film, "casting")
    (crew,) = await _by_type(session, film, "crew_attached")
    (removal,) = await _by_type(session, film, CREDIT_REMOVED_EVENT_TYPE)
    assert (crew.status, crew.superseded_by) == ("superseded", removal.id)
    assert (casting.status, casting.superseded_by) == ("published", None)


async def test_a_removal_marks_each_named_person_and_nobody_else(session, session_factory, run_id):
    """Per person, matched by `subject_key`: a third person's card on the film is untouched."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    director = await _person(session, 100, "Denis Villeneuve")
    lead = await _person(session, 200, "Timothée Chalamet")
    stays = await _person(session, 300, "Zendaya")
    await _attached(session, film, director)
    await _cast(session, film, lead)
    await _cast(session, film, stays)
    await session.commit()
    await _run(session_factory, run_id)
    await _attached(session, film, director, change=CREDIT_REMOVED, changed_at=NOW)
    await _cast(session, film, lead, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    await _run_detachment(session_factory, run_id)

    (crew,) = await _by_type(session, film, "crew_attached")
    (casting,) = await _by_type(session, film, "casting")
    (removal,) = await _by_type(session, film, CREDIT_REMOVED_EVENT_TYPE)
    assert set(removal.subject_key or []) == {"denis villeneuve", "timothée chalamet"}
    assert (crew.status, crew.superseded_by) == ("superseded", removal.id)
    # The casting card names both the departing lead and the performer who stays. The card is
    # the unit of supersession (D-2), so it is marked — Zendaya's *own* claim lives on it.
    assert (casting.status, casting.superseded_by) == ("superseded", removal.id)


async def test_an_attachment_card_after_the_removal_is_not_superseded(
    session, session_factory, run_id
):
    """Only cards whose `occurred_at` precedes the removal are candidates."""
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "Denis Villeneuve")
    await _attached(session, film, person, changed_at=YESTERDAY)
    await session.commit()
    await _run(session_factory, run_id)
    later_card = Event(
        film_id=film.id,
        event_type="crew_attached",
        confidence="rumored",
        provenance="story",
        occurred_at=NOW + timedelta(hours=1),
        region=None,
        subject_key=["denis villeneuve"],
    )
    session.add(later_card)
    await session.flush()
    await _attached(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    await _run_detachment(session_factory, run_id)

    (removal,) = await _by_type(session, film, CREDIT_REMOVED_EVENT_TYPE)
    cards = {e.occurred_at: e for e in await _by_type(session, film, "crew_attached")}
    assert (cards[YESTERDAY].status, cards[YESTERDAY].superseded_by) == ("superseded", removal.id)
    assert (cards[later_card.occurred_at].status, cards[later_card.occurred_at].superseded_by) == (
        "published",
        None,
    )


async def test_two_departures_sharing_one_card_do_not_reach_an_older_card(
    session, session_factory, run_id
):
    """Two people on one casting card leave together: that card is marked once, and an older
    trade-story card one of them is also on stays published — it is not the claim this
    removal corrects. (Marking mid-loop would autoflush the first update and let the second
    name's query fall through to the older card.)"""
    film = await add_film(session, 1, release_date=None, status="Planned")
    lead = await _person(session, 100, "Timothée Chalamet")
    costar = await _person(session, 200, "Zendaya")
    older_story_card = Event(
        film_id=film.id,
        event_type="casting",
        confidence="rumored",
        provenance="story",
        occurred_at=YESTERDAY - timedelta(days=30),
        region=None,
        subject_key=["zendaya"],
    )
    session.add(older_story_card)
    await session.flush()
    await session.commit()
    # Zendaya is already carded by the story, so the catalog card below names only Chalamet
    # unless we bypass suppression: write the shared card directly.
    shared = Event(
        film_id=film.id,
        event_type="casting",
        confidence="rumored",
        provenance="catalog",
        occurred_at=YESTERDAY,
        region=None,
        subject_key=["timothée chalamet", "zendaya"],
    )
    session.add(shared)
    await session.flush()
    await _cast(session, film, lead, change=CREDIT_REMOVED, changed_at=NOW)
    await _cast(session, film, costar, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    await _run_detachment(session_factory, run_id)

    (removal,) = await _by_type(session, film, CREDIT_REMOVED_EVENT_TYPE)
    cards = {e.id: e for e in await _by_type(session, film, "casting")}
    assert (cards[shared.id].status, cards[shared.id].superseded_by) == ("superseded", removal.id)
    assert (cards[older_story_card.id].status, cards[older_story_card.id].superseded_by) == (
        "published",
        None,
    )


# ── Tier-A short-circuit, sweep side (NEU-1371, ADR-0017 D-5) ──────────────


STORY_CONFIRM_DAYS = 14


async def _story_card(
    session, film, *, names, occurred_at, event_type="casting", provenance="story"
):
    """A published card naming `names`. The trades' half of the short-circuit: what the
    cluster stage leaves behind after a story about a casting is clustered."""
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="rumored",
        provenance=provenance,
        occurred_at=occurred_at,
        region=None,
        subject_key=list(names),
    )
    session.add(event)
    await session.flush()
    return event


async def _short_circuited(session_factory, run_id, **overrides):
    return await _run(
        session_factory,
        run_id,
        quarantine_hours=QUARANTINE_HOURS,
        story_confirm_days=STORY_CONFIRM_DAYS,
        **overrides,
    )


async def _change(session, film, person):
    return (
        await session.execute(
            select(FilmCreditChange).where(
                FilmCreditChange.film_id == film.id,
                FilmCreditChange.person_id == person.id,
                FilmCreditChange.change == CREDIT_ADDED,
            ),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def test_a_credit_the_trades_broke_first_is_published_by_that_card(
    session, session_factory, run_id
):
    """The backward half of INV-4: story day 0, credit in TMDB day 2. The change is stamped
    at load time and cards nothing — exactly one card exists, the one the trades ran."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Zendaya")
    card = await _story_card(session, film, names=["zendaya"], occurred_at=NOW - timedelta(days=5))
    await _cast(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person, credit_type="cast", job=None, credit_order=0)
    await session.commit()

    result = await _short_circuited(session_factory, run_id)

    assert (await _change(session, film, person)).carded_by_event_id == card.id
    # Dropped before the backlog is read, so it is not a held row and not a skipped group.
    assert (result.story_published, result.attachments_read, result.held) == (1, 0, 0)
    assert result.events_created == 0
    assert [e.id for e in await _events(session, film)] == [card.id]


async def test_a_director_story_publishes_a_crew_attached_change(session, session_factory, run_id):
    """The type-scoping trap. The LLM has no `crew_attached` in its vocabulary, so a story
    about a director attaching is carded `casting` — and a `crew_attached` group would never
    find it by type. Matching on *who* is what closes this."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    card = await _story_card(
        session, film, names=["denis villeneuve"], occurred_at=NOW - timedelta(days=5)
    )
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()

    result = await _short_circuited(session_factory, run_id)

    assert (await _change(session, film, person)).carded_by_event_id == card.id
    assert result.story_published == 1
    assert [e.id for e in await _events(session, film)] == [card.id]


async def test_a_story_about_someone_else_leaves_the_change_alone(session, session_factory, run_id):
    """Per-person, not per-film: a film gains cast repeatedly, and one story is never
    confirmation of every credit TMDB has pending on it."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _story_card(session, film, names=["zendaya"], occurred_at=NOW - timedelta(days=5))
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()

    result = await _short_circuited(session_factory, run_id)

    assert (await _change(session, film, person)).carded_by_event_id is None
    assert (result.story_published, result.events_created) == (0, 1)
    (card,) = await _by_type(session, film, "crew_attached")
    assert card.provenance == "catalog"


async def test_a_story_older_than_the_window_does_not_publish_the_change(
    session, session_factory, run_id
):
    """A card from long before the credit landed was reporting something else — a previous
    attachment, or a rumour TMDB has only now caught up to by coincidence."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _story_card(
        session,
        film,
        names=["denis villeneuve"],
        occurred_at=AGED - timedelta(days=STORY_CONFIRM_DAYS + 1),
    )
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()

    result = await _short_circuited(session_factory, run_id)

    assert (await _change(session, film, person)).carded_by_event_id is None
    assert (result.story_published, result.events_created) == (0, 1)


async def test_a_catalog_card_does_not_publish_a_pending_change(session, session_factory, run_id):
    """Only *story* cards short-circuit. A catalog card naming the person is the sweep's own
    earlier work, and letting it retire a later attachment would silently swallow a second,
    genuine credit — which is what the phase's own suppression is for."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _story_card(
        session,
        film,
        names=["denis villeneuve"],
        occurred_at=NOW - timedelta(days=5),
        provenance="catalog",
        event_type="crew_attached",
    )
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()

    result = await _short_circuited(session_factory, run_id)

    assert (await _change(session, film, person)).carded_by_event_id is None
    assert result.story_published == 0


async def test_a_published_change_is_never_read_again(session, session_factory, run_id):
    """The stamp is the whole suppression: once written, the row leaves the backlog for good
    and is counted once, not on every pass for as long as the window holds it."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Zendaya")
    await _story_card(session, film, names=["zendaya"], occurred_at=NOW - timedelta(days=5))
    await _cast(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person, credit_type="cast", job=None, credit_order=0)
    await session.commit()

    first = await _short_circuited(session_factory, run_id)
    second = await _short_circuited(session_factory, run_id)

    assert (first.story_published, second.story_published) == (1, 0)
    assert (second.attachments_read, second.events_created, second.skipped) == (0, 0, 0)
    assert len(await _events(session, film)) == 1


async def test_zero_disables_the_short_circuit(session, session_factory, run_id):
    """`0` restores the pre-NEU-1371 behaviour: the story card and the catalog card both
    exist. Kept as a switch because it is the rollback, and because it is what every caller
    with no `Settings` gets."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    await _story_card(
        session, film, names=["denis villeneuve"], occurred_at=NOW - timedelta(days=5)
    )
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    await session.commit()

    result = await _run(
        session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, story_confirm_days=0
    )

    assert (await _change(session, film, person)).carded_by_event_id is None
    assert (result.story_published, result.events_created) == (0, 1)
    assert len(await _events(session, film)) == 2


async def test_a_published_credit_that_is_later_removed_still_cards_the_removal(
    session, session_factory, run_id
):
    """The detachment half is unaffected (NEU-1200/1205): the removal gate asks for a prior
    *visible* attachment card of any provenance, and the story card is one. It supersedes
    that card, per NEU-1347."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Zendaya")
    card = await _story_card(session, film, names=["zendaya"], occurred_at=NOW - timedelta(days=5))
    await _cast(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person, credit_type="cast", job=None, credit_order=0)
    await session.commit()
    await _short_circuited(session_factory, run_id)

    # TMDB drops the credit again. The live row goes with it, the way a rebuild leaves it.
    await session.execute(delete(FilmCredit).where(FilmCredit.film_id == film.id))
    await _cast(session, film, person, change=CREDIT_REMOVED, changed_at=NOW)
    await session.commit()

    await _run_detachment(session_factory, run_id)

    (removal,) = await _by_type(session, film, CREDIT_REMOVED_EVENT_TYPE)
    assert removal.subject_key == ["zendaya"]
    published = await session.get(Event, card.id, populate_existing=True)
    assert published is not None
    assert (published.status, published.superseded_by) == ("superseded", removal.id)


async def test_a_held_change_is_not_published_by_a_story_until_the_hold_lifts(
    session, session_factory, run_id
):
    """The short-circuit honours D-8. A held row is one a human or a condition still has to
    decide about, and the stamp is permanent — retiring it here would leave `reconcile_holds`
    expiring a hold over a change that could no longer card whatever was decided. Deferring
    costs nothing: the release puts the row back in front of the same story card."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Denis Villeneuve")
    card = await _story_card(
        session, film, names=["denis villeneuve"], occurred_at=NOW - timedelta(days=5)
    )
    await _attached(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person)
    hold = CreditHold(
        film_id=film.id,
        person_id=person.id,
        credit_type="director",
        changed_at=AGED,
        reason="deceased",
        held_at=NOW,
    )
    session.add(hold)
    await session.commit()

    held_pass = await _short_circuited(session_factory, run_id)

    assert (await _change(session, film, person)).carded_by_event_id is None
    assert (held_pass.story_published, held_pass.events_created) == (0, 0)

    # An admin looks at it and lets it through. The story card is still the publication.
    await credit_holds.release_hold(session, hold_id=hold.id)
    await session.commit()
    released_pass = await _short_circuited(session_factory, run_id)

    assert (await _change(session, film, person)).carded_by_event_id == card.id
    assert (released_pass.story_published, released_pass.events_created) == (1, 0)
    assert [e.id for e in await _events(session, film)] == [card.id]


async def test_the_earliest_story_card_is_the_one_that_published_the_change(
    session, session_factory, run_id
):
    """Which card is stamped is the "we had it first" answer (§5), so it must be the scoop —
    the trade that broke the beat — not the rest of the trades repeating it days later."""
    film = await add_film(session, 1)
    person = await _person(session, 100, "Zendaya")
    scoop = await _story_card(session, film, names=["zendaya"], occurred_at=NOW - timedelta(days=6))
    await _story_card(session, film, names=["zendaya"], occurred_at=NOW - timedelta(days=1))
    await _cast(session, film, person, changed_at=AGED)
    await _still_attached(session, film, person, credit_type="cast", job=None, credit_order=0)
    await session.commit()

    result = await _short_circuited(session_factory, run_id)

    assert (await _change(session, film, person)).carded_by_event_id == scoop.id
    assert result.story_published == 1


# ── The recorded grade: a followed person's non-seed credits (D-49) ───────────


async def _follows(session, user, person_id: int) -> None:
    from upmovies.app.models import Follow

    session.add(
        Follow(
            user_id=user.id,
            entity_type="person",
            entity_id=str(person_id),
            source="manual",
        )
    )
    await session.flush()


async def test_a_followed_persons_minor_cast_credit_cards_as_casting(
    session, session_factory, run_id, make_user
):
    """The beat D-49 exists for: a 12th-billed credit is not seed grade, so it only reaches
    the history because somebody follows the person at `any` — and from there it goes through
    the same quarantine and grouping as a lead's."""
    user = await make_user(email="wide@example.com")
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "A Supporting Actor")
    await _follows(session, user, 100)
    await add_credit(session, film, 100, credit_type="cast", credit_order=11)
    await _cast(session, film, person, changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert (result.attachments_read, result.events_created, result.held) == (1, 1, 0)
    (event,) = await _events(session, film)
    assert event.event_type == "casting"
    assert event.subject_key == ["a supporting actor"]
    assert (await _summary(session, event)).summary == "A Supporting Actor joins the cast."


async def test_a_followed_persons_non_seed_crew_credit_cards_as_crew_attached(
    session, session_factory, run_id, make_user
):
    """A cinematographer carries no seed grade at all, so `credit_role` answers None for them
    and the phase used to drop the row. `recorded_role` answers `crew`, which
    `CREDIT_ROLE_EVENT_TYPES` cards as `crew_attached` beside a director's."""
    user = await make_user(email="wide@example.com")
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "A Cinematographer")
    await _follows(session, user, 100)
    await add_credit(
        session, film, 100, credit_type="crew", job="Cinematographer", department="Camera"
    )
    await _attached(session, film, person, job="Cinematographer", changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.event_type == "crew_attached"
    assert (await _summary(session, event)).summary == "A Cinematographer joins the crew."


async def test_a_director_and_a_followed_crew_member_share_one_card(
    session, session_factory, run_id, make_user
):
    """Burst grouping is by (film, event type, pass), so a cinematographer's attachment joins
    a director's in the same pass — one beat, one card, the body naming both by role."""
    user = await make_user(email="wide@example.com")
    film = await add_film(session, 1, release_date=None, status="Planned")
    director = await _person(session, 100, "A Director")
    dop = await _person(session, 101, "A Cinematographer")
    await _follows(session, user, 101)
    await add_credit(session, film, 100, credit_type="crew", job="Director", department="Directing")
    await add_credit(
        session, film, 101, credit_type="crew", job="Cinematographer", department="Camera"
    )
    await _attached(session, film, director, changed_at=AGED)
    await _attached(session, film, dop, job="Cinematographer", changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.event_type == "crew_attached"
    assert (await _summary(session, event)).summary == (
        "A Director attached to direct. A Cinematographer joins the crew."
    )


async def test_the_quarantine_gate_reads_a_followed_persons_credit_as_still_present(
    session, session_factory, run_id, make_user
):
    """The gate asks "is this credit still in `catalog.film_credit` under the same recorded
    role". Asked under seed grade alone, a followed person's non-seed credit is never present,
    so every one of them would be held forever and nothing would ever card."""
    user = await make_user(email="wide@example.com")
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "A Supporting Actor")
    await _follows(session, user, 100)
    await add_credit(session, film, 100, credit_type="cast", credit_order=11)
    await _cast(session, film, person, changed_at=AGED)
    await session.commit()

    assert (await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)).held == 0


async def test_a_reverted_minor_credit_is_still_held(session, session_factory, run_id, make_user):
    """The other half of the gate: widening what counts as present must not stop it noticing
    an edit TMDB has taken back. No live credit row at all, so nothing publishes."""
    user = await make_user(email="wide@example.com")
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "A Supporting Actor")
    await _follows(session, user, 100)
    await _cast(session, film, person, changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert (result.events_created, result.held) == (0, 1)
    assert await _events(session, film) == []


async def test_a_dropped_follow_stops_carding_the_credit_it_recorded(
    session, session_factory, run_id, make_user
):
    """The quarantine gate and the credit-history diff read recorded grade from the *same*
    live follow set, and that agreement is the load-bearing property: a gate that judged a
    credit present under a rule the diff no longer records it under would publish an
    attachment while the next ingest wrote its removal.

    So a follow dropped while its credit is still in quarantine takes the pending attachment
    with it — held, never carded, and it ages out of the window. That is the narrower reading
    of D-49's "the gate checks presence, not the follow": presence is still a property of the
    film and of no user's preferences *at read time*, but what counts as a recorded credit is
    one definition shared with the writer, not two.

    Unfollowing is the only way to make this happen since EF-1 — there is no tier left to
    narrow — and a 12th-billed credit is the case that shows it, because seed grade would
    carry it on its own if recorded grade were not what the gate reads.
    """
    from upmovies.app.models import Follow

    user = await make_user(email="wide@example.com")
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "A Supporting Actor")
    await _follows(session, user, 100)
    await add_credit(session, film, 100, credit_type="cast", credit_order=11)
    await _cast(session, film, person, changed_at=AGED)
    await session.commit()

    follow = await session.get(Follow, (user.id, "person", "100"))
    assert follow is not None
    await session.delete(follow)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert (result.events_created, result.held) == (0, 1)


async def test_a_reverted_crew_job_is_held_even_when_another_job_survives(
    session, session_factory, run_id, make_user
):
    """`crew` folds every non-seed job into one role, so the presence check has to compare the
    *job* (`role_match_key`). Without that, a reverted `Gaffer` credit reads as still there on
    the strength of an unrelated `Best Boy` one — the exact edit quarantine suppresses."""
    user = await make_user(email="wide@example.com")
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "A Sparks")
    await _follows(session, user, 100)
    await add_credit(session, film, 100, credit_type="crew", job="Best Boy", department="Lighting")
    await _attached(session, film, person, job="Gaffer", changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert (result.events_created, result.held) == (0, 1)


async def test_two_crew_jobs_in_one_edit_name_the_person_once(
    session, session_factory, run_id, make_user
):
    """A recorded credit is identified by `(person, credit_type, job)`, so picking up two
    non-seed crew jobs in one edit is two rows at one role — and a body reading "X and X join
    the crew" is what the per-role dedupe in `group_attachments` exists to stop."""
    user = await make_user(email="wide@example.com")
    film = await add_film(session, 1, release_date=None, status="Planned")
    person = await _person(session, 100, "A Sparks")
    await _follows(session, user, 100)
    for job in ("Gaffer", "Best Boy"):
        await add_credit(session, film, 100, credit_type="crew", job=job, department="Lighting")
        await _attached(session, film, person, job=job, changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert (result.attachments_read, result.events_created) == (2, 1)
    (event,) = await _events(session, film)
    assert event.subject_key == ["a sparks"]
    assert (await _summary(session, event)).summary == "A Sparks joins the crew."
