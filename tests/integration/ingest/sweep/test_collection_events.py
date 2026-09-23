"""The sweep's collection phase end to end (EF-5, NEU-1434): which `collection_id` rows of
`catalog.film_field_change` become cards, what quarantine withholds, and what a departure does
to the arrival card it corrects.

The rule carrying the most weight is the one not implemented here at all — first observation is
a baseline (§5.3), which the `film_field_change_trg` trigger guarantees upstream by being a
`BEFORE UPDATE` trigger: admitting a film writes no history row, so a catalog full of films
already filed under franchises cannot card. It is asserted anyway, because this is the phase
where getting it wrong would surface.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import Collection, FilmFieldChange
from upmovies.ingest.sweep import run_collection_events
from upmovies.news.models import Event, EventStory, EventSummary, Story, StoryEntity
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL

NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
LOOKBACK_DAYS = 7
QUARANTINE_HOURS = 72
# Old enough to have cleared a 72h hold, young enough to still be inside the 7-day window.
AGED = NOW - timedelta(hours=QUARANTINE_HOURS + 1)
OLDER = NOW - timedelta(hours=QUARANTINE_HOURS + 30)

DUNE = 726871
ALIEN = 8091


async def _collection(session, collection_id: int, name: str) -> Collection:
    collection = Collection(id=collection_id, name=name)
    session.add(collection)
    await session.flush()
    return collection


async def _change(session, film, *, old, new, changed_at=AGED):
    """One `collection_id` history row, written directly — the trigger's own output, minus a
    TMDB refresh to produce it."""
    session.add(
        FilmFieldChange(
            film_id=film.id,
            field="collection_id",
            old_value=old,
            new_value=new,
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
    }
    return await run_collection_events(**{**kwargs, **overrides})


async def _events(session, film):
    return (
        (
            await session.execute(
                select(Event).where(Event.film_id == film.id).order_by(Event.created_at),
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


# --- the three transitions -----------------------------------------------------------------


async def test_joining_a_franchise_cards_a_rumored_catalog_event(session, session_factory, run_id):
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 1, collection_id=DUNE)
    await _change(session, film, old=None, new=DUNE)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.event_type == "collection_attached"
    assert event.confidence == "rumored"
    assert event.provenance == "catalog"
    assert event.occurred_at == AGED
    assert event.subject_key == [f"collection:{DUNE}"]


async def test_leaving_a_franchise_cards_a_removal(session, session_factory, run_id):
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 2)
    await _change(session, film, old=DUNE, new=None)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.event_type == "collection_removed"
    assert event.subject_key == [f"collection:{DUNE}"]


async def test_a_move_cards_a_departure_and_an_arrival(session, session_factory, run_id):
    """One history row, two beats — the transition no other phase has."""
    await _collection(session, DUNE, "Dune Collection")
    await _collection(session, ALIEN, "Alien Collection")
    film = await add_film(session, 3, collection_id=ALIEN)
    await _change(session, film, old=DUNE, new=ALIEN)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.changes_read == 2
    assert result.events_created == 2
    events = {e.event_type: e for e in await _events(session, film)}
    assert events["collection_removed"].subject_key == [f"collection:{DUNE}"]
    assert events["collection_attached"].subject_key == [f"collection:{ALIEN}"]


async def test_the_card_carries_a_deterministic_summary(session, session_factory, run_id):
    """Every read path inner-joins `event_summary`, so a card written without one is invisible
    on every surface."""
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 4, collection_id=DUNE)
    await _change(session, film, old=None, new=DUNE)
    await session.commit()

    await _run(session_factory, run_id)

    (event,) = await _events(session, film)
    summary = await _summary(session, event)
    assert summary.model == DETERMINISTIC_MODEL
    assert summary.summary == "The film joins the Dune Collection."


async def test_a_film_with_no_history_rows_cards_nothing(session, session_factory, run_id):
    """First observation is a baseline: admitting a film writes no history row, so a film that
    arrived already filed under a franchise has nothing here to read."""
    await _collection(session, DUNE, "Dune Collection")
    await add_film(session, 5, collection_id=DUNE)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.changes_read == 0
    assert result.events_created == 0


async def test_other_tracked_fields_are_not_this_phase_s(session, session_factory, run_id):
    """`status` shares the table and is `field_events`' to card."""
    film = await add_film(session, 6)
    session.add(
        FilmFieldChange(
            film_id=film.id,
            field="status",
            old_value="Planned",
            new_value="In Production",
            changed_at=AGED,
        )
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.changes_read == 0
    assert await _events(session, film) == []


async def test_a_change_outside_the_lookback_window_is_not_read(session, session_factory, run_id):
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 7, collection_id=DUNE)
    await _change(session, film, old=None, new=DUNE, changed_at=NOW - timedelta(days=30))
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.changes_read == 0


async def test_a_collection_with_no_catalog_row_is_skipped(session, session_factory, run_id):
    """The body needs a name, and a card with a hole in it is worse than no card."""
    film = await add_film(session, 8)
    await _change(session, film, old=999999, new=None)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.changes_read == 0
    assert await _events(session, film) == []


# --- quarantine ----------------------------------------------------------------------------


async def test_a_change_inside_the_quarantine_window_is_held(session, session_factory, run_id):
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 9, collection_id=DUNE)
    await _change(session, film, old=None, new=DUNE, changed_at=NOW - timedelta(hours=1))
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.held == 1
    assert result.events_created == 0


async def test_an_arrival_the_catalog_no_longer_agrees_with_is_held(
    session, session_factory, run_id
):
    """The live-state half of the gate: the film has to still hold the collection at
    publication, or the arrival never happened."""
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 10)  # collection_id left NULL — the edit was reverted
    await _change(session, film, old=None, new=DUNE)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.held == 1
    assert result.events_created == 0


async def test_a_departure_the_catalog_no_longer_agrees_with_is_held(
    session, session_factory, run_id
):
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 11, collection_id=DUNE)  # still filed under it
    await _change(session, film, old=DUNE, new=None)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.held == 1
    assert result.events_created == 0


async def test_a_departure_survives_a_move_on_to_a_third_franchise(
    session, session_factory, run_id
):
    """Live state is one value, so "still off it" is a *difference*, not an absence: the film
    really has left Dune even though it now holds Alien."""
    await _collection(session, DUNE, "Dune Collection")
    await _collection(session, ALIEN, "Alien Collection")
    film = await add_film(session, 12, collection_id=ALIEN)
    await _change(session, film, old=DUNE, new=ALIEN)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    types = {e.event_type for e in await _events(session, film)}
    assert types == {"collection_removed", "collection_attached"}
    assert result.held == 0


async def test_a_reverted_arrival_publishes_nothing_in_either_direction(
    session, session_factory, run_id
):
    """The round trip: a film filed under a franchise and pulled straight back out must not
    card an arrival (live state disagrees) *or* a departure (nobody was told it joined)."""
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 13)
    await _change(session, film, old=None, new=DUNE, changed_at=OLDER)
    await _change(session, film, old=DUNE, new=None, changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert await _events(session, film) == []
    assert result.events_created == 0


async def test_a_reverted_departure_publishes_nothing_in_either_direction(
    session, session_factory, run_id
):
    """The mirror, and the case the studio half does not cover: a cleared `collection_id` put
    back is an arrival at a franchise the film was never visibly seen to leave."""
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 14, collection_id=DUNE)
    await _change(session, film, old=DUNE, new=None, changed_at=OLDER)
    await _change(session, film, old=None, new=DUNE, changed_at=AGED)
    await session.commit()

    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert await _events(session, film) == []
    assert result.events_created == 0


async def test_a_re_arrival_after_a_carded_departure_is_real_news(session, session_factory, run_id):
    """The revert gate is conditioned on there being no card at all. Once the departure has
    published, the film coming back is something the reader is owed."""
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 15)
    await _change(session, film, old=DUNE, new=None, changed_at=OLDER)
    await session.commit()
    await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    film.collection_id = DUNE
    await _change(session, film, old=None, new=DUNE, changed_at=AGED)
    await session.commit()
    result = await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    assert result.events_created == 1
    assert [e.event_type for e in await _events(session, film)] == [
        "collection_removed",
        "collection_attached",
    ]


# --- supersession and re-reads -------------------------------------------------------------


async def test_a_departure_supersedes_the_arrival_card_it_corrects(
    session, session_factory, run_id
):
    """D-2: the arrival keeps its place on every surface, marked as the claim this corrects."""
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 16, collection_id=DUNE)
    await _change(session, film, old=None, new=DUNE, changed_at=OLDER)
    await session.commit()
    await _run(session_factory, run_id)

    film.collection_id = None
    await _change(session, film, old=DUNE, new=None, changed_at=AGED)
    await session.commit()
    await _run(session_factory, run_id)

    arrival, departure = await _events(session, film)
    assert arrival.event_type == "collection_attached"
    assert arrival.status == "superseded"
    assert arrival.superseded_by == departure.id
    assert departure.status == "published"


async def test_a_departure_with_no_arrival_card_supersedes_nothing(
    session, session_factory, run_id
):
    """A film filed under a franchise since its baseline has no arrival card, and its
    departure is a complete beat on its own — there is no prior-arrival gate."""
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 17)
    await _change(session, film, old=DUNE, new=None)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.events_created == 1
    (event,) = await _events(session, film)
    assert event.event_type == "collection_removed"
    assert event.status == "published"


async def test_re_reading_the_same_window_cards_nothing_more(session, session_factory, run_id):
    """The rolling window is the queue, so every pass re-reads what the last one carded."""
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 18, collection_id=DUNE)
    await _change(session, film, old=None, new=DUNE)
    await session.commit()
    await _run(session_factory, run_id)

    result = await _run(session_factory, run_id)

    assert result.events_created == 0
    assert result.skipped == 1
    assert len(await _events(session, film)) == 1


async def test_a_move_away_from_an_uncarded_arrival_only_cards_the_new_franchise(
    session, session_factory, run_id
):
    """The move whose *old* arrival is still inside the window. That arrival never published
    — it is held for good now that the film has moved on — so announcing the departure would
    name a franchise no reader was told the film had joined. Only the arrival at the new one
    is news."""
    await _collection(session, DUNE, "Dune Collection")
    await _collection(session, ALIEN, "Alien Collection")
    film = await add_film(session, 19, collection_id=ALIEN)
    await _change(session, film, old=None, new=DUNE, changed_at=OLDER)
    await _change(session, film, old=DUNE, new=ALIEN, changed_at=AGED)
    await session.commit()

    await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    events = await _events(session, film)
    assert [e.event_type for e in events] == ["collection_attached"]
    assert events[0].subject_key == [f"collection:{ALIEN}"]


async def test_a_move_away_from_a_carded_arrival_cards_the_departure_too(
    session, session_factory, run_id
):
    """The same move once the old arrival *has* published: the reader knows the film was
    filed under Dune, so its departure is owed — and it supersedes the card that said so."""
    await _collection(session, DUNE, "Dune Collection")
    await _collection(session, ALIEN, "Alien Collection")
    film = await add_film(session, 20, collection_id=DUNE)
    await _change(session, film, old=None, new=DUNE, changed_at=OLDER)
    await session.commit()
    await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    film.collection_id = ALIEN
    await _change(session, film, old=DUNE, new=ALIEN, changed_at=AGED)
    await session.commit()
    await _run(session_factory, run_id, quarantine_hours=QUARANTINE_HOURS)

    arrival, departure, moved_to = await _events(session, film)
    assert arrival.subject_key == [f"collection:{DUNE}"]
    assert arrival.status == "superseded"
    assert arrival.superseded_by == departure.id
    assert departure.event_type == "collection_removed"
    assert moved_to.subject_key == [f"collection:{ALIEN}"]


async def _story_card_with_mention(
    session, film, *, collection_id: int, event_type: str = "collection_attached", occurred_at=OLDER
) -> Event:
    """A franchise story card as the cluster and resolve stages leave it: no `subject_key` of
    its own, and a resolved `news.story_entity` row on its story naming the collection."""
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="rumored",
        provenance="story",
        occurred_at=occurred_at,
    )
    session.add(event)
    await session.flush()
    story = Story(source="Deadline", url=f"https://deadline.test/{event.id}", title="Story")
    session.add(story)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    session.add(
        StoryEntity(
            story_id=story.id,
            kind="collection",
            entity_id=collection_id,
            name_as_written="The Dune Collection",
            path="accepted",
            features={"title_mentioned": None, "event_type": event_type},
            prompt_version="1",
        )
    )
    await session.flush()
    return event


async def test_a_franchise_the_trades_broke_first_cards_once(session, session_factory, run_id):
    """EF-13 end to end for franchises. The phase stamps the `collection_id` row before reading
    its own backlog, so the row never enters it and the trades' card is the only one."""
    await _collection(session, DUNE, "Dune Collection")
    # Filed at insert rather than by an UPDATE: the `film_field_change_trg` trigger is
    # `BEFORE UPDATE`, so assigning the column afterwards would write a *second* history row
    # and this test is about what one row does.
    film = await add_film(session, 70, collection_id=DUNE)
    card = await _story_card_with_mention(session, film, collection_id=DUNE)
    await _change(session, film, old=None, new=DUNE)
    await session.commit()

    result = await _run(
        session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, story_confirm_days=14
    )

    assert (result.story_published, result.changes_read, result.events_created) == (1, 0, 0)
    assert [e.id for e in await _events(session, film)] == [card.id]


async def test_a_move_is_carded_by_the_sweep_despite_the_story(session, session_factory, run_id):
    """The move carve-out, from the carding side. One history row is two beats and has one
    stamp column between them, so it is never stamped — and both cards are raised, which is
    what stops a franchise the film really did leave going unreported.

    The story's own card stands beside them; ADR-0014 promotion is what reconciles the pair,
    not this phase."""
    await _collection(session, ALIEN, "Alien Collection")
    await _collection(session, DUNE, "Dune Collection")
    film = await add_film(session, 71, collection_id=DUNE)
    await _story_card_with_mention(session, film, collection_id=DUNE)
    await _change(session, film, old=ALIEN, new=DUNE)
    await session.commit()

    result = await _run(
        session_factory, run_id, quarantine_hours=QUARANTINE_HOURS, story_confirm_days=14
    )

    assert result.story_published == 0
    assert (result.changes_read, result.events_created) == (2, 2)
    stamped = (
        await session.execute(
            select(FilmFieldChange.carded_by_event_id).where(FilmFieldChange.film_id == film.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert stamped is None
