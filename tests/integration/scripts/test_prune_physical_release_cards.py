"""The one-off repair that takes the US physical date off `release_date` cards (NEU-1542,
D-1542.6).

It runs against production once and deletes rows, so what matters is the blast radius: a
physical-only card goes with its summary and digest rows; a mixed card keeps its other markets,
loses the token and has its body re-rendered from the change rows — unless someone wrote that
body by hand; a story-provenance card and the change history are untouched.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from scripts.prune_physical_release_cards import (
    DELETED,
    KEPT_EDITED,
    KEPT_NO_CHANGES,
    RE_RENDERED,
    prune,
)
from tests.fixtures.catalog import add_film
from upmovies.app.models import Notification
from upmovies.catalog.models import Film, FilmReleaseDateChange
from upmovies.news.models import Event, EventSummary

OBSERVED = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)


async def _change(session, film: Film, *, release_type: int, new: date) -> None:
    session.add(
        FilmReleaseDateChange(
            film_id=film.id,
            iso_3166_1="US",
            release_type=release_type,
            previous_date=None,
            new_date=new,
            change="set",
            changed_at=OBSERVED,
        )
    )


async def _card(
    session,
    film: Film,
    *,
    tokens: list[str] | None,
    body: str,
    provenance: str = "catalog",
    edited: bool = False,
) -> Event:
    event = Event(
        film_id=film.id,
        event_type="release_date",
        confidence="confirmed",
        provenance=provenance,
        occurred_at=OBSERVED,
        region="US",
        subject_key=tokens,
    )
    session.add(event)
    await session.flush()
    session.add(
        EventSummary(
            event_id=event.id,
            summary=body,
            model="deterministic",
            prompt_version="deterministic-9",
            source_updated_at=OBSERVED,
            edited_at=OBSERVED + timedelta(days=1) if edited else None,
        )
    )
    await session.flush()
    return event


async def _count(session, model, *where) -> int:
    stmt = select(func.count()).select_from(model).where(*where)
    return (await session.execute(stmt)).scalar_one()


async def _summary(session, event: Event) -> str:
    return (
        await session.execute(
            select(EventSummary.summary).where(EventSummary.event_id == event.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def _tokens(session, event: Event) -> list[str] | None:
    return (
        await session.execute(
            select(Event.subject_key).where(Event.id == event.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def _physical_only(session, make_user) -> Event:
    user = await make_user(email="tom@example.com")
    film = await add_film(session, 9601, title="Disc Only")
    await _change(session, film, release_type=5, new=date(2026, 12, 1))
    event = await _card(
        session,
        film,
        tokens=["US:physical"],
        body="US physical release date set to 1 December 2026.",
    )
    session.add(
        Notification(
            user_id=user.id, event_id=event.id, kind="digest", channel="email", status="sent"
        )
    )
    return event


async def _mixed(session, *, tmdb_id: int = 9602, edited: bool = False) -> Event:
    film = await add_film(session, tmdb_id, title=f"Mixed {tmdb_id}")
    await _change(session, film, release_type=3, new=date(2026, 10, 2))
    await _change(session, film, release_type=5, new=date(2026, 12, 1))
    return await _card(
        session,
        film,
        tokens=["US:wide", "US:physical"],
        body="US wide release date set to 2 October 2026. US physical release date set to "
        "1 December 2026.",
        edited=edited,
    )


async def test_a_dry_run_reports_every_card_and_writes_nothing(session, make_user):
    physical = await _physical_only(session, make_user)
    mixed = await _mixed(session)
    await session.commit()

    pruned = await prune(session, apply=False)

    by_id = {c.event_id: c for c in pruned.cards}
    assert (by_id[physical.id].outcome, by_id[physical.id].tokens_after) == (DELETED, [])
    assert [status for _, status, _ in by_id[physical.id].notifications] == ["sent"]
    assert by_id[mixed.id].outcome == RE_RENDERED
    assert by_id[mixed.id].tokens_after == ["US:wide"]
    assert by_id[mixed.id].body_after == "US wide release date set to 2 October 2026."
    assert (pruned.deleted, pruned.updated) == ({}, 0)
    assert await _count(session, Event) == 2
    assert await _tokens(session, mixed) == ["US:wide", "US:physical"]


async def test_apply_deletes_a_physical_only_card_with_its_digest_rows(session, make_user):
    physical = await _physical_only(session, make_user)
    await session.commit()

    pruned = await prune(session, apply=True)

    assert pruned.deleted == {"event_story": 0, "event_summary": 1, "event": 1}
    assert await _count(session, Event, Event.id == physical.id) == 0
    assert await _count(session, Notification, Notification.event_id == physical.id) == 0
    # The history stays; the sweep no longer reads a non-displayable type.
    assert await _count(session, FilmReleaseDateChange) == 1


async def test_apply_re_renders_a_mixed_card_from_its_change_rows(session):
    mixed = await _mixed(session)
    await session.commit()

    pruned = await prune(session, apply=True)

    assert pruned.updated == 1
    assert await _tokens(session, mixed) == ["US:wide"]
    assert await _summary(session, mixed) == "US wide release date set to 2 October 2026."


async def test_a_hand_edited_mixed_card_keeps_its_body_and_loses_the_token(session):
    mixed = await _mixed(session, edited=True)
    await session.commit()
    before = await _summary(session, mixed)

    pruned = await prune(session, apply=True)

    assert pruned.totals[KEPT_EDITED] == 1
    assert await _tokens(session, mixed) == ["US:wide"]
    assert await _summary(session, mixed) == before


async def test_a_mixed_card_without_its_change_rows_keeps_its_body(session):
    film = await add_film(session, 9603, title="No History")
    mixed = await _card(
        session,
        film,
        tokens=["US:wide", "US:physical"],
        body="US wide release date set to 2 October 2026. US physical release date set to "
        "1 December 2026.",
    )
    await session.commit()
    before = await _summary(session, mixed)

    pruned = await prune(session, apply=True)

    assert pruned.totals[KEPT_NO_CHANGES] == 1
    assert await _tokens(session, mixed) == ["US:wide"]
    assert await _summary(session, mixed) == before


async def test_a_story_card_and_a_card_without_the_token_are_untouched(session):
    film = await add_film(session, 9604, title="Untouched")
    story = await _card(
        session, film, tokens=None, body="A trade says the disc is out.", provenance="story"
    )
    digital = await _card(
        session, film, tokens=["US:digital"], body="US digital release date set to 1 May 2027."
    )
    await session.commit()

    pruned = await prune(session, apply=True)

    assert pruned.cards == []
    assert await _count(session, Event, Event.id.in_([story.id, digital.id])) == 2
    assert await _tokens(session, digital) == ["US:digital"]


async def test_a_second_run_finds_nothing(session, make_user):
    await _physical_only(session, make_user)
    await _mixed(session)
    await session.commit()
    await prune(session, apply=True)

    assert (await prune(session, apply=False)).cards == []


async def test_a_card_left_with_an_origin_country_move_is_tagged_with_that_market(session):
    """The sweep tags a card `US` whenever a US move is in it, so a physical + origin-country
    card was `US`; with the physical token gone it is the origin market's card."""
    film = await add_film(session, 9605, title="Abroad", origin_country=["FR"])
    session.add(
        FilmReleaseDateChange(
            film_id=film.id,
            iso_3166_1="FR",
            release_type=3,
            previous_date=None,
            new_date=date(2026, 10, 2),
            change="set",
            changed_at=OBSERVED,
        )
    )
    card = await _card(
        session,
        film,
        tokens=["FR:wide", "US:physical"],
        body="FR wide release date set to 2 October 2026. US physical release date set to "
        "1 December 2026.",
    )
    await session.commit()

    await prune(session, apply=True)

    region = (
        await session.execute(
            select(Event.region).where(Event.id == card.id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert (await _tokens(session, card), region) == (["FR:wide"], "FR")
    assert await _summary(session, card) == "FR wide release date set to 2 October 2026."
