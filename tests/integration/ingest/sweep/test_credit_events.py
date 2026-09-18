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

from tests.fixtures.catalog import add_credit, add_film
from upmovies.catalog.models import FilmCredit, FilmCreditChange, Person
from upmovies.ingest.sweep import credit_events, run_credit_attachment_events
from upmovies.ingest.sweep.credit_events import (
    _card_detachment_group,
    run_credit_detachment_events,
)
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
QUARANTINE_HOURS = 72
# Old enough to have cleared a 72h hold, young enough to still be inside the 7-day window —
# the band every quarantine test that expects a card has to sit in.
AGED = NOW - timedelta(hours=QUARANTINE_HOURS + 1)


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
