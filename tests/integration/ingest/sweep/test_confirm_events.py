"""The sweep's confirmation phase (EF-10, D-1446.4): a story card the catalog has caught up
with stops being a rumor.

The flip is the writer NEU-1438's push window has been waiting on — the `updated_at` arm it
spelled and armed against nothing — so these tests assert both halves of it: `confidence` moves
to `confirmed`, and `updated_at` moves to the pass's own `now`. A card whose change TMDB has
since reverted must do neither, and must still be a candidate on the pass after that.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_credit, add_film
from upmovies.catalog.models import (
    Collection,
    Film,
    FilmCompanyChange,
    FilmCreditChange,
    FilmFieldChange,
    FilmProductionCompany,
    Person,
    ProductionCompany,
)
from upmovies.ingest.sweep import confirm_stamped_cards, run_confirmation_events
from upmovies.news.models import Event, EventStory, Story, StoryEntity
from upmovies.news.subject_key import collection_subject_token, company_subject_token

NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
QUARANTINE_HOURS = 72
QUARANTINE = timedelta(hours=QUARANTINE_HOURS)
AGED = NOW - QUARANTINE - timedelta(hours=1)
"""Old enough to have cleared the window."""
FRESH = NOW - timedelta(hours=1)
"""Still inside it."""
PUBLISHED_AT = NOW - timedelta(days=5)

COMPANY = 923
COLLECTION = 726871
DIRECTOR = 525

ORG_ARMS = [
    pytest.param("company", COMPANY, id="company"),
    pytest.param("collection", COLLECTION, id="collection"),
]


def _attach_type(kind: str) -> str:
    return f"{kind}_attached"


def _detach_type(kind: str) -> str:
    return f"{kind}_removed"


def _token(kind: str, entity_id: int) -> str:
    return (company_subject_token if kind == "company" else collection_subject_token)(entity_id)


async def _card(
    session,
    film,
    *,
    event_type: str,
    provenance: str = "story",
    confidence: str = "rumored",
    subject_key: list[str] | None = None,
    occurred_at: datetime = PUBLISHED_AT,
    status: str = "published",
) -> Event:
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence=confidence,
        provenance=provenance,
        status=status,
        occurred_at=occurred_at,
        subject_key=subject_key,
    )
    session.add(event)
    await session.flush()
    event.updated_at = PUBLISHED_AT
    await session.flush()
    return event


async def _entity(session, kind: str, entity_id: int) -> None:
    if kind == "company":
        session.add(ProductionCompany(id=entity_id, name="Legendary Pictures"))
    else:
        session.add(Collection(id=entity_id, name="The Dune Collection"))
    await session.flush()


async def _stamped_change(
    session, film, *, kind: str, entity_id: int, card: Event, direction: str, changed_at=AGED
) -> None:
    if kind == "company":
        session.add(
            FilmCompanyChange(
                film_id=film.id,
                company_id=entity_id,
                change=direction,
                changed_at=changed_at,
                carded_by_event_id=card.id,
            )
        )
    else:
        session.add(
            FilmFieldChange(
                film_id=film.id,
                field="collection_id",
                old_value=None if direction == "added" else entity_id,
                new_value=entity_id if direction == "added" else None,
                changed_at=changed_at,
                carded_by_event_id=card.id,
            )
        )
    await session.flush()


async def _hold(session, film, *, kind: str, entity_id: int) -> None:
    if kind == "company":
        session.add(FilmProductionCompany(film_id=film.id, company_id=entity_id))
    else:
        film.collection_id = entity_id
    await session.flush()


async def _mention(session, card: Event, *, kind: str, entity_id: int, event_type: str) -> None:
    """A resolved `story_entity` row on one of the card's stories — the only way a story card
    ever names an organisation, since `subject_key` is written before resolution runs."""
    story = Story(source="Deadline", url=f"https://deadline.test/{card.id}", title="Story")
    session.add(story)
    await session.flush()
    session.add(EventStory(event_id=card.id, story_id=story.id))
    session.add(
        StoryEntity(
            story_id=story.id,
            kind=kind,
            entity_id=entity_id,
            name_as_written="Legendary Pictures",
            path="accepted",
            features={"title_mentioned": None, "event_type": event_type},
            prompt_version="1",
        )
    )
    await session.flush()


async def _confirm(session, *, now: datetime = NOW):
    return await confirm_stamped_cards(session, now=now, quarantine=QUARANTINE)


async def _reread(session, card: Event) -> Event:
    return (
        await session.execute(
            select(Event).where(Event.id == card.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_an_aged_attachment_the_catalog_still_holds_confirms_its_card(
    session, kind, entity_id
):
    """The beat EF-10 promises. The trade broke it, TMDB confirmed it, the window has passed
    and the attachment is still standing — so the rumor becomes fact, and `updated_at` moves so
    the notify pass's reopened window sees it."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    card = await _card(session, film, event_type=_attach_type(kind))
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=card, direction="added"
    )
    await _hold(session, film, kind=kind, entity_id=entity_id)
    await session.commit()

    assert await _confirm(session) == (1, 1, 0)
    await session.commit()
    flipped = await _reread(session, card)
    assert flipped.confidence == "confirmed"
    assert flipped.updated_at == NOW


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_row_still_inside_the_window_is_not_a_candidate(session, kind, entity_id):
    """The stamp runs the moment the change is observed, before the window — confirming there
    would publish a push for something quarantine exists to let TMDB take back."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    card = await _card(session, film, event_type=_attach_type(kind))
    await _stamped_change(
        session,
        film,
        kind=kind,
        entity_id=entity_id,
        card=card,
        direction="added",
        changed_at=FRESH,
    )
    await _hold(session, film, kind=kind, entity_id=entity_id)
    await session.commit()

    assert await _confirm(session) == (0, 0, 0)
    assert (await _reread(session, card)).confidence == "rumored"


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_reverted_change_confirms_nothing_and_flips_on_a_later_pass(
    session, kind, entity_id
):
    """TMDB took the attachment back inside the window. The card stays a rumor and the row
    stays stamped — un-stamping it would let the sweep card a duplicate — so the same row is a
    candidate again next pass, and confirms if the attachment returns."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    card = await _card(session, film, event_type=_attach_type(kind))
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=card, direction="added"
    )
    await session.commit()

    assert await _confirm(session) == (1, 0, 0)
    assert (await _reread(session, card)).confidence == "rumored"

    await _hold(session, film, kind=kind, entity_id=entity_id)
    await session.commit()

    assert await _confirm(session) == (1, 1, 0)
    assert (await _reread(session, card)).confidence == "confirmed"


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_an_already_confirmed_card_is_never_selected(session, kind, entity_id):
    """Idempotent by construction rather than by a marker, which is what makes a second pass
    over the same rolling window free — and what stops `updated_at` being bumped daily, which
    the notify window would read as a fresh upgrade every single day."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    card = await _card(session, film, event_type=_attach_type(kind), confidence="confirmed")
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=card, direction="added"
    )
    await _hold(session, film, kind=kind, entity_id=entity_id)
    await session.commit()

    assert await _confirm(session) == (0, 0, 0)
    assert (await _reread(session, card)).updated_at == PUBLISHED_AT


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_catalog_card_is_never_selected(session, kind, entity_id):
    """A catalog attach card published only after quarantine and is confirmed by construction
    for the push decision (EF-8). This phase is for the other provenance."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    card = await _card(
        session,
        film,
        event_type=_attach_type(kind),
        provenance="catalog",
        subject_key=[_token(kind, entity_id)],
    )
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=card, direction="added"
    )
    await _hold(session, film, kind=kind, entity_id=entity_id)
    await session.commit()

    assert await _confirm(session) == (0, 0, 0)


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_confirmed_detachment_supersedes_the_attach_card_it_contradicts(
    session, kind, entity_id
):
    """D-1446.5. As a rumor a trade's word supersedes nothing — a wrong report would hide a
    true attachment — but once the catalog agrees the entity has gone, the card saying it
    joined is no longer the current claim."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    attach = await _card(
        session,
        film,
        event_type=_attach_type(kind),
        provenance="catalog",
        confidence="confirmed",
        subject_key=[_token(kind, entity_id)],
        occurred_at=PUBLISHED_AT - timedelta(days=30),
    )
    detach = await _card(session, film, event_type=_detach_type(kind))
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=detach, direction="removed"
    )
    await session.commit()

    assert await _confirm(session) == (1, 1, 1)
    await session.commit()
    assert (await _reread(session, detach)).confidence == "confirmed"
    superseded = await _reread(session, attach)
    assert superseded.status == "superseded"
    assert superseded.superseded_by == detach.id


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_rumored_detachment_supersedes_nothing(session, kind, entity_id):
    """The same shape, still inside the window: nothing is marked until the catalog agrees."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    attach = await _card(
        session,
        film,
        event_type=_attach_type(kind),
        provenance="catalog",
        confidence="confirmed",
        subject_key=[_token(kind, entity_id)],
        occurred_at=PUBLISHED_AT - timedelta(days=30),
    )
    detach = await _card(session, film, event_type=_detach_type(kind))
    await _stamped_change(
        session,
        film,
        kind=kind,
        entity_id=entity_id,
        card=detach,
        direction="removed",
        changed_at=FRESH,
    )
    await session.commit()

    assert await _confirm(session) == (0, 0, 0)
    assert (await _reread(session, attach)).status == "published"


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_detachment_the_catalog_disagrees_with_confirms_nothing(session, kind, entity_id):
    """A `removed` row confirms only when the attachment is still *gone*. The entity is back on
    the film, so the departure was a flap and the card waits."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    detach = await _card(session, film, event_type=_detach_type(kind))
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=detach, direction="removed"
    )
    await _hold(session, film, kind=kind, entity_id=entity_id)
    await session.commit()

    assert await _confirm(session) == (1, 0, 0)
    assert (await _reread(session, detach)).confidence == "rumored"


async def test_a_person_card_confirms_on_the_credit_still_standing(session):
    """The person arm, which D-1446.3 brought along rather than leaving for a later ticket: a
    credit a trade broke and TMDB confirmed flips the card the same way."""
    film = await add_film(session, 1)
    session.add(Person(id=DIRECTOR, name="Denis Villeneuve"))
    await session.flush()
    card = await _card(session, film, event_type="crew_attached", subject_key=["denis villeneuve"])
    await add_credit(session, film, DIRECTOR, credit_type="crew", job="Director")
    session.add(
        FilmCreditChange(
            film_id=film.id,
            person_id=DIRECTOR,
            credit_type="crew",
            job="Director",
            change="added",
            changed_at=AGED,
            carded_by_event_id=card.id,
        )
    )
    await session.commit()

    assert await _confirm(session) == (1, 1, 0)
    assert (await _reread(session, card)).confidence == "confirmed"


async def test_a_person_card_whose_credit_was_reverted_stays_a_rumor(session):
    film = await add_film(session, 1)
    session.add(Person(id=DIRECTOR, name="Denis Villeneuve"))
    await session.flush()
    card = await _card(session, film, event_type="crew_attached", subject_key=["denis villeneuve"])
    session.add(
        FilmCreditChange(
            film_id=film.id,
            person_id=DIRECTOR,
            credit_type="crew",
            job="Director",
            change="added",
            changed_at=AGED,
            carded_by_event_id=card.id,
        )
    )
    await session.commit()

    assert await _confirm(session) == (1, 0, 0)
    assert (await _reread(session, card)).confidence == "rumored"


async def test_one_card_named_by_two_changes_is_flipped_once(session):
    """A story naming two studios cards once; TMDB then observes the two attachments as two
    rows. Both stamp the same card, and the card is flipped once at one `updated_at` — a smear
    would make the notify window see two upgrades."""
    film = await add_film(session, 1)
    await _entity(session, "company", COMPANY)
    await _entity(session, "company", COMPANY + 1)
    card = await _card(session, film, event_type="company_attached")
    for entity_id in (COMPANY, COMPANY + 1):
        await _stamped_change(
            session, film, kind="company", entity_id=entity_id, card=card, direction="added"
        )
        await _hold(session, film, kind="company", entity_id=entity_id)
    await session.commit()

    assert await _confirm(session) == (2, 1, 0)
    assert (await _reread(session, card)).updated_at == NOW


async def test_a_move_is_read_past(session):
    """A move is never stamped in the first place — one row, two beats, one stamp column — so a
    stamped row always resolves to one direction. Guarded anyway, because the stamp is durable
    and the rule that writes it is not."""
    film = await add_film(session, 1)
    await _entity(session, "collection", COLLECTION)
    await _entity(session, "collection", COLLECTION + 1)
    card = await _card(session, film, event_type="collection_attached")
    session.add(
        FilmFieldChange(
            film_id=film.id,
            field="collection_id",
            old_value=COLLECTION + 1,
            new_value=COLLECTION,
            changed_at=AGED,
            carded_by_event_id=card.id,
        )
    )
    film.collection_id = COLLECTION
    await session.commit()

    assert await _confirm(session) == (0, 0, 0)
    assert (await _reread(session, card)).confidence == "rumored"


async def test_the_phase_reports_what_it_read_and_wrote(session, session_factory, run_id):
    """The counts land on the sweep's detail line beside every other phase's."""
    film = await add_film(session, 1)
    await _entity(session, "company", COMPANY)
    card = await _card(session, film, event_type="company_attached")
    await _stamped_change(
        session, film, kind="company", entity_id=COMPANY, card=card, direction="added"
    )
    await _hold(session, film, kind="company", entity_id=COMPANY)
    await session.commit()

    result = await run_confirmation_events(
        session_factory=session_factory,
        run_id=run_id,
        now=NOW,
        quarantine_hours=QUARANTINE_HOURS,
        failure_threshold=10,
    )

    assert (result.stamped_read, result.cards_confirmed, result.cards_superseded) == (1, 1, 0)
    assert (result.failures, result.aborted) == (0, False)
    assert (await _reread(session, card)).confidence == "confirmed"


async def test_a_zero_quarantine_confirms_on_the_pass_that_stamped(session):
    """`SWEEP_CREDIT_QUARANTINE_HOURS = 0` turns the wait off, exactly as it does in the
    carding phases: every stamped row is eligible immediately."""
    film = await add_film(session, 1)
    await _entity(session, "company", COMPANY)
    card = await _card(session, film, event_type="company_attached")
    await _stamped_change(
        session,
        film,
        kind="company",
        entity_id=COMPANY,
        card=card,
        direction="added",
        changed_at=NOW,
    )
    await _hold(session, film, kind="company", entity_id=COMPANY)
    await session.commit()

    assert await confirm_stamped_cards(session, now=NOW, quarantine=timedelta(0)) == (1, 1, 0)


async def test_an_unstamped_change_is_nothing_to_this_phase(session):
    """The stamp is the whole input. A change nobody carded from a story is the sweep's own to
    card, and carries no rumor to confirm."""
    film = await add_film(session, 1)
    await _entity(session, "company", COMPANY)
    session.add(
        FilmCompanyChange(film_id=film.id, company_id=COMPANY, change="added", changed_at=AGED)
    )
    await _hold(session, film, kind="company", entity_id=COMPANY)
    await session.commit()

    assert await _confirm(session) == (0, 0, 0)
    assert (await session.execute(select(Film.id))).scalars().all() == [film.id]


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_a_confirmed_detachment_supersedes_a_story_attach_card_too(session, kind, entity_id):
    """The card a *story* raised is the one this ticket newly makes possible, and it carries no
    `company:<id>` token — resolution runs after clustering. Matching the prior attach card on
    tokens alone would leave it published beside a confirmed contradiction, which is exactly
    what D-1446.5 rejected. It is found by its resolved mention instead."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    attach = await _card(
        session,
        film,
        event_type=_attach_type(kind),
        confidence="confirmed",
        occurred_at=PUBLISHED_AT - timedelta(days=30),
    )
    await _mention(session, attach, kind=kind, entity_id=entity_id, event_type=_attach_type(kind))
    detach = await _card(session, film, event_type=_detach_type(kind))
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=detach, direction="removed"
    )
    await session.commit()

    assert await _confirm(session) == (1, 1, 1)
    await session.commit()
    superseded = await _reread(session, attach)
    assert superseded.status == "superseded"
    assert superseded.superseded_by == detach.id


@pytest.mark.parametrize(("kind", "entity_id"), ORG_ARMS)
async def test_another_entitys_story_attach_card_is_not_superseded(session, kind, entity_id):
    """The mention is what names the card, so a card about a different studio on the same film
    is untouched — the token branch was exact and this branch has to be too."""
    film = await add_film(session, 1)
    await _entity(session, kind, entity_id)
    await _entity(session, kind, entity_id + 1)
    other = await _card(
        session,
        film,
        event_type=_attach_type(kind),
        confidence="confirmed",
        occurred_at=PUBLISHED_AT - timedelta(days=30),
    )
    await _mention(
        session, other, kind=kind, entity_id=entity_id + 1, event_type=_attach_type(kind)
    )
    detach = await _card(session, film, event_type=_detach_type(kind))
    await _stamped_change(
        session, film, kind=kind, entity_id=entity_id, card=detach, direction="removed"
    )
    await session.commit()

    assert await _confirm(session) == (1, 1, 0)
    assert (await _reread(session, other)).status == "published"
