"""The backward half of D-5's stamp for studios and franchises (EF-13, D-1446.6).

The person half is covered where it is exercised, in `tests/integration/ingest/sweep/
test_credit_events.py`, because for people the stamp is only observable through the phase whose
backlog it empties. The organisation half is tested directly here: its two kinds read two
different change tables with two different notions of *direction*, and most of what can go
wrong — a kind crossing, a mention that never resolved, a move being stamped — is a property of
the stamper rather than of any one carding phase.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import (
    Collection,
    FilmCompanyChange,
    FilmFieldChange,
    ProductionCompany,
)
from upmovies.news.attachment_confirm import stamp_prior_story_cards
from upmovies.news.models import Event, EventStory, Story, StoryEntity

NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
WITHIN_DAYS = 14
SINCE = NOW - timedelta(days=7)
CHANGED_AT = NOW - timedelta(days=2)
COMPANY = 923
COLLECTION = 726871

ORG_ARMS = [
    pytest.param("company", COMPANY, id="company"),
    pytest.param("collection", COLLECTION, id="collection"),
]


def _attach_type(kind: str) -> str:
    return f"{kind}_attached"


def _detach_type(kind: str) -> str:
    return f"{kind}_removed"


async def _story_card(
    session,
    film,
    *,
    kind: str,
    entity_id: int | None,
    event_type: str | None = None,
    mention_type: str | None = None,
    path: str = "accepted",
    occurred_at: datetime = NOW - timedelta(days=5),
) -> Event:
    """A story-formed organisation card with one resolved mention on its story — the shape the
    cluster stage plus the resolve stage leave behind. No `subject_key`: a story card cannot
    carry an organisation token, because resolution runs after clustering."""
    card_type = event_type or _attach_type(kind)
    event = Event(
        film_id=film.id,
        event_type=card_type,
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
            kind=kind,
            entity_id=entity_id,
            name_as_written="Legendary Pictures",
            path=path,
            features={
                "title_mentioned": None,
                "event_type": mention_type or card_type,
            },
            prompt_version="1",
        )
    )
    await session.flush()
    return event


async def _change(
    session, film, *, kind: str, entity_id: int, direction: str, changed_at=CHANGED_AT
):
    if kind == "company":
        session.add(ProductionCompany(id=entity_id, name="Legendary Pictures"))
        await session.flush()
        session.add(
            FilmCompanyChange(
                film_id=film.id, company_id=entity_id, change=direction, changed_at=changed_at
            )
        )
    else:
        session.add(Collection(id=entity_id, name="The Dune Collection"))
        await session.flush()
        session.add(
            FilmFieldChange(
                film_id=film.id,
                field="collection_id",
                old_value=None if direction == "added" else entity_id,
                new_value=entity_id if direction == "added" else None,
                changed_at=changed_at,
            )
        )
    await session.flush()


async def _stamp(session, kind: str) -> int:
    return await stamp_prior_story_cards(
        session, since=SINCE, within_days=WITHIN_DAYS, kinds=(kind,)
    )


async def _stamped(session, kind: str):
    model = FilmCompanyChange if kind == "company" else FilmFieldChange
    return (
        (
            await session.execute(
                select(model.carded_by_event_id), execution_options={"populate_existing": True}
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_change_the_trades_broke_first_is_stamped_with_that_card(session, kind, entity_id):
    """The ordinary case the organisation arms exist for: the trade ran the beat, TMDB caught
    up days later, and the change must publish *as that card* rather than raise a second one."""
    film = await add_film(session, 1)
    card = await _story_card(session, film, kind=kind, entity_id=entity_id)
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await _stamp(session, kind) == 1
    assert await _stamped(session, kind) == [card.id]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_the_earliest_qualifying_card_is_the_one_stamped(session, kind, entity_id):
    """Two cards about one attachment: the earlier is the scoop and the later the trades
    repeating it. `carded_by_event_id` answers *who had it first*, so it has to point at the
    scoop — stamping the corroboration would make a Monday beat read as a Thursday one."""
    film = await add_film(session, 1)
    scoop = await _story_card(
        session, film, kind=kind, entity_id=entity_id, occurred_at=NOW - timedelta(days=5)
    )
    await _story_card(
        session, film, kind=kind, entity_id=entity_id, occurred_at=NOW - timedelta(days=3)
    )
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await _stamp(session, kind) == 1
    assert await _stamped(session, kind) == [scoop.id]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_detachment_is_stamped_against_the_detach_card(session, kind, entity_id):
    """Direction is part of the key. A story reporting that a studio *left* a film has not
    published the row recording that it joined, and vice versa."""
    film = await add_film(session, 1)
    detach = await _story_card(
        session, film, kind=kind, entity_id=entity_id, event_type=_detach_type(kind)
    )
    await _change(session, film, kind=kind, entity_id=entity_id, direction="removed")
    await session.commit()

    assert await _stamp(session, kind) == 1
    assert await _stamped(session, kind) == [detach.id]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_an_attach_card_does_not_stamp_a_departure(session, kind, entity_id):
    film = await add_film(session, 1)
    await _story_card(session, film, kind=kind, entity_id=entity_id)
    await _change(session, film, kind=kind, entity_id=entity_id, direction="removed")
    await session.commit()

    assert await _stamp(session, kind) == 0
    assert await _stamped(session, kind) == [None]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
@pytest.mark.parametrize("path", ["unlinked", "not_in_tmdb"])
async def test_an_unresolved_mention_names_nobody(session, kind, entity_id, path):
    """D-25's cut. A mention the resolve stage could not tie to a TMDB row is not evidence that
    this card was about this studio, whatever id happens to sit beside it."""
    film = await add_film(session, 1)
    await _story_card(session, film, kind=kind, entity_id=entity_id, path=path)
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await _stamp(session, kind) == 0
    assert await _stamped(session, kind) == [None]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_mention_of_another_beat_does_not_stamp(session, kind, entity_id):
    """The mention's own `event_type` has to agree with the card's. "Legendary's *Dune*" on an
    attach card is a possessive, not a claim that Legendary has just boarded anything."""
    film = await add_film(session, 1)
    await _story_card(session, film, kind=kind, entity_id=entity_id, mention_type="other")
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await _stamp(session, kind) == 0
    assert await _stamped(session, kind) == [None]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_card_outside_the_window_does_not_stamp(session, kind, entity_id):
    """`within_days` is measured from the change's own `changed_at`: a card from long before
    the change landed was reporting something else."""
    film = await add_film(session, 1)
    await _story_card(
        session, film, kind=kind, entity_id=entity_id, occurred_at=CHANGED_AT - timedelta(days=40)
    )
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await _stamp(session, kind) == 0
    assert await _stamped(session, kind) == [None]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_card_that_landed_after_the_change_still_stamps_it(session, kind, entity_id):
    """Bounded below only, on the person half's terms: a card that published *after* the change
    still published the beat, and is the ordinary shape whenever the change was observed first
    and the story arrived a day later — which is exactly the pass this stamp saves."""
    film = await add_film(session, 1)
    card = await _story_card(
        session, film, kind=kind, entity_id=entity_id, occurred_at=CHANGED_AT + timedelta(hours=6)
    )
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await _stamp(session, kind) == 1
    assert await _stamped(session, kind) == [card.id]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_change_older_than_the_sweep_window_is_not_read(session, kind, entity_id):
    """`since` is the sweep's rolling window. Rows older than it are carded by nothing, so
    stamping them would be writing history nothing reads."""
    film = await add_film(session, 1)
    await _story_card(session, film, kind=kind, entity_id=entity_id)
    await _change(
        session,
        film,
        kind=kind,
        entity_id=entity_id,
        direction="added",
        changed_at=SINCE - timedelta(days=1),
    )
    await session.commit()

    assert await _stamp(session, kind) == 0
    assert await _stamped(session, kind) == [None]


async def test_the_kinds_do_not_cross(session):
    """`story_entity` holds both organisation kinds in one table, so a collection mention whose
    `entity_id` happens to equal a company id must not stamp that company's change."""
    film = await add_film(session, 1)
    await _story_card(
        session,
        film,
        kind="collection",
        entity_id=COMPANY,
        event_type="company_attached",
        mention_type="company_attached",
    )
    await _change(session, film, kind="company", entity_id=COMPANY, direction="added")
    await session.commit()

    assert await _stamp(session, "company") == 0
    assert await _stamped(session, "company") == [None]


async def test_a_franchise_move_is_never_stamped(session):
    """One history row, two beats, one stamp column. Stamping a move for its arrival would take
    the row out of the sweep's backlog and suppress the *departure* card with it — a franchise
    the film really did leave, silently unreported."""
    film = await add_film(session, 1)
    session.add(Collection(id=COLLECTION, name="The Dune Collection"))
    session.add(Collection(id=COLLECTION + 1, name="Another Collection"))
    await session.flush()
    await _story_card(session, film, kind="collection", entity_id=COLLECTION)
    session.add(
        FilmFieldChange(
            film_id=film.id,
            field="collection_id",
            old_value=COLLECTION + 1,
            new_value=COLLECTION,
            changed_at=CHANGED_AT,
        )
    )
    await session.commit()

    assert await _stamp(session, "collection") == 0
    assert await _stamped(session, "collection") == [None]


async def test_a_status_field_change_is_never_read(session):
    """`film_field_change` carries every tracked column; only the `collection_id` rows are in
    scope. A status change has no organisation to resolve and its own once-per-film suppression
    (`sweep.field_events._already_carded`)."""
    film = await add_film(session, 1)
    await _story_card(session, film, kind="collection", entity_id=COLLECTION)
    session.add(
        FilmFieldChange(
            film_id=film.id,
            field="status",
            old_value="Planned",
            new_value="In Production",
            changed_at=CHANGED_AT,
        )
    )
    await session.commit()

    assert await _stamp(session, "collection") == 0
    assert await _stamped(session, "collection") == [None]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_an_already_stamped_row_is_not_restamped(session, kind, entity_id):
    """The loader reads `carded_by_event_id IS NULL`, so a stamped row is out of the backlog
    for good — and a second pass over the same window must not move the attribution."""
    film = await add_film(session, 1)
    scoop = await _story_card(session, film, kind=kind, entity_id=entity_id)
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await _stamp(session, kind) == 1
    await session.commit()
    assert await _stamp(session, kind) == 0
    assert await _stamped(session, kind) == [scoop.id]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_each_phase_stamps_only_its_own_kind(session, kind, entity_id):
    """Three carders run per sweep and each calls this before reading its backlog, so the
    stamper is scoped by kind the way `load_change_backlog` is scoped by field — otherwise one
    pass would do the same work three times and report it three times over."""
    film = await add_film(session, 1)
    await _story_card(session, film, kind=kind, entity_id=entity_id)
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    other = "collection" if kind == "company" else "company"
    assert await _stamp(session, other) == 0
    assert await _stamped(session, kind) == [None]
    assert await _stamp(session, kind) == 1


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_zero_window_stamps_nothing(session, kind, entity_id):
    """`SWEEP_STORY_CONFIRM_DAYS = 0` turns the short-circuit off outright, for both halves."""
    film = await add_film(session, 1)
    await _story_card(session, film, kind=kind, entity_id=entity_id)
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await stamp_prior_story_cards(session, since=SINCE, within_days=0, kinds=(kind,)) == 0
    assert await _stamped(session, kind) == [None]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_superseded_card_does_not_stamp(session, kind, entity_id):
    """A stamped row leaves the carding backlog for good, so stamping it against a card that is
    no longer published would retire the change with nothing standing in for it — the beat
    would simply never be carded at all. Reachable now that a story can form an attach card and
    a later confirmed detachment can supersede it (D-1446.5)."""
    film = await add_film(session, 1)
    card = await _story_card(session, film, kind=kind, entity_id=entity_id)
    card.status = "superseded"
    await _change(session, film, kind=kind, entity_id=entity_id, direction="added")
    await session.commit()

    assert await _stamp(session, kind) == 0
    assert await _stamped(session, kind) == [None]
