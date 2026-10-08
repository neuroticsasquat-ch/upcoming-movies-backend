"""The one-off repair that removes `now_available` cards pre-orders raised (NEU-1538, D-1538.5).

It runs against production once and is not reversible, so what matters is the blast radius: a
card raised before its film's US digital date goes with its summary, story links, digest rows
and — unlike the trailer prune — the ledger rows its observation inserted, since leaving those
keeps the film silent for the real release. A card raised on or after the date stays, ledger
and all.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import respx
from sqlalchemy import func, select

from scripts.prune_preorder_availability import prune
from tests.fixtures.catalog import add_film
from tests.fixtures.tmdb import make_watch_providers
from upmovies.app.models import Notification
from upmovies.catalog.models import AvailabilityFirstSeen, Film, FilmReleaseDate, WatchProvider
from upmovies.ingest import runs
from upmovies.ingest.providers import run_provider_poll
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.news.models import Event, EventStory, EventSummary, Story

BASE_URL = "https://api.themoviedb.org/3"
# The poll's observation of each card, stamped explicitly so the gate judges a fixed day.
OBSERVED = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
FANDANGO = 7
APPLE = 2


async def _release(session, film: Film, on: date, *, release_type: int) -> None:
    session.add(
        FilmReleaseDate(
            film_id=film.id,
            iso_3166_1="US",
            release_type=release_type,
            release_date=datetime.combine(on, datetime.min.time(), tzinfo=UTC),
        )
    )
    await session.flush()


async def _digital(session, film: Film, on: date) -> None:
    await _release(session, film, on, release_type=4)


async def _ledger_row(session, film: Film, provider_id: int, kind: str, at: datetime) -> None:
    if await session.get(WatchProvider, provider_id) is None:
        session.add(WatchProvider(id=provider_id, name=f"Provider {provider_id}"))
        await session.flush()
    session.add(
        AvailabilityFirstSeen(
            film_id=film.id,
            region="US",
            provider_id=provider_id,
            monetization_type=kind,
            first_seen_at=at,
        )
    )
    await session.flush()


async def _ledger_providers(session, film: Film) -> list[int]:
    rows = await session.execute(
        select(AvailabilityFirstSeen.provider_id)
        .where(AvailabilityFirstSeen.film_id == film.id)
        .order_by(AvailabilityFirstSeen.id)
    )
    return list(rows.scalars())


async def _card(session, film: Film, *, offers: list[tuple[int, str]]) -> Event:
    """A `now_available` card on `film` as the pre-gate poll wrote it: the event at `OBSERVED`,
    its summary, and the ledger rows its observation inserted, stamped the same instant."""
    for provider_id, kind in offers:
        await _ledger_row(session, film, provider_id, kind, OBSERVED)
    event = Event(
        film_id=film.id,
        event_type="now_available",
        confidence="confirmed",
        provenance="catalog",
        occurred_at=OBSERVED,
        region="US",
        subject_key=sorted({f"US:{kind}" for _, kind in offers}),
    )
    session.add(event)
    await session.flush()
    session.add(
        EventSummary(
            event_id=event.id,
            summary="Available to buy.",
            model="deterministic",
            prompt_version="v1",
            source_updated_at=OBSERVED,
        )
    )
    await session.flush()
    return event


async def _count(session, model, *where) -> int:
    stmt = select(func.count()).select_from(model).where(*where)
    return (await session.execute(stmt)).scalar_one()


async def _fixture(session, make_user):
    """A pre-order card (no US digital date) with a story link and a digest row, beside a
    legitimate card on another film whose date was before the observation."""
    user = await make_user(email="tom@example.com")
    preorder_film = await add_film(session, 1283515, status="Released")
    legit_film = await add_film(session, 9601, status="Released")
    await _digital(session, legit_film, OBSERVED.date() - timedelta(days=5))
    preorder = await _card(session, preorder_film, offers=[(FANDANGO, "buy")])
    legit = await _card(session, legit_film, offers=[(APPLE, "rent"), (APPLE, "buy")])
    story = Story(
        source="google_news",
        url="https://example.com/preorder",
        title="Out at home now, says nobody",
        film_id=preorder_film.id,
        link_status="linked",
        link_confidence=0.9,
        linked_at=OBSERVED,
    )
    session.add(story)
    await session.flush()
    session.add(EventStory(event_id=preorder.id, story_id=story.id))
    for event in (preorder, legit):
        session.add(
            Notification(
                user_id=user.id, event_id=event.id, kind="digest", channel="email", status="sent"
            )
        )
    await session.commit()
    return preorder_film, legit_film, preorder, legit, user.id


async def test_a_dry_run_reports_the_preorder_card_and_deletes_nothing(session, make_user):
    film, _, preorder, _, user_id = await _fixture(session, make_user)

    pruned = await prune(session, apply=False)

    (card,) = pruned.cards
    assert pruned.deleted == {}
    assert (card.event_id, card.title, card.tmdb_id, card.us_digital) == (
        preorder.id,
        film.title,
        1283515,
        None,
    )
    assert (card.occurred_at, card.subject_key) == (OBSERVED, ["US:buy"])
    assert card.providers == [("buy", "Provider 7")]
    # By user id: the dry run's output may go on the PR, and emails do not.
    assert [(uid, status) for uid, status, _ in card.notifications] == [(user_id, "sent")]
    assert await _count(session, Event) == 2
    assert await _count(session, AvailabilityFirstSeen) == 3


async def test_apply_removes_the_card_and_the_ledger_rows_behind_it(session, make_user):
    film, legit_film, preorder, legit, _ = await _fixture(session, make_user)

    pruned = await prune(session, apply=True)

    assert pruned.deleted == {
        "event_story": 1,
        "event_summary": 1,
        "event": 1,
        "availability_first_seen": 1,
    }
    assert await _count(session, Event, Event.id == preorder.id) == 0
    assert await _count(session, EventSummary, EventSummary.event_id == preorder.id) == 0
    assert await _count(session, EventStory, EventStory.event_id == preorder.id) == 0
    assert await _count(session, Notification, Notification.event_id == preorder.id) == 0
    assert (
        await _count(session, AvailabilityFirstSeen, AvailabilityFirstSeen.film_id == film.id) == 0
    )
    # The legitimate card on another film keeps everything.
    assert await _count(session, Event, Event.id == legit.id) == 1
    assert await _count(session, Notification, Notification.event_id == legit.id) == 1
    assert (
        await _count(session, AvailabilityFirstSeen, AvailabilityFirstSeen.film_id == legit_film.id)
        == 2
    )


async def test_a_card_raised_before_a_later_digital_date_is_selected(session):
    """TMDB has since dated the film, after the day the poll believed the pre-order."""
    film = await add_film(session, 9602, status="Released")
    await _digital(session, film, OBSERVED.date() + timedelta(days=10))
    event = await _card(session, film, offers=[(FANDANGO, "buy")])
    await session.commit()

    (card,) = (await prune(session, apply=False)).cards

    assert card.event_id == event.id
    assert card.us_digital == OBSERVED.date() + timedelta(days=10)


async def test_a_card_raised_on_its_digital_date_is_kept(session):
    """The gate is inclusive of the day (D-1538.1): availability landing on its announced date
    is the beat itself."""
    film = await add_film(session, 9603, status="Released")
    await _digital(session, film, OBSERVED.date())
    await _card(session, film, offers=[(APPLE, "rent")])
    await session.commit()

    assert (await prune(session, apply=False)).cards == []


@respx.mock
async def test_the_next_poll_cards_a_pruned_film_once_its_date_has_passed(session, session_factory):
    """The D-1538.5 round trip, and the reason the ledger rows go: with them left in place the
    real release would find `buy` already known and card nothing, ever."""
    today = OBSERVED.date() + timedelta(days=14)
    film = await add_film(session, 9604, status="Released")
    # A wide US opening inside the poll's age window, so the film is in the scoped set.
    await _release(session, film, today - timedelta(days=30), release_type=3)
    await _card(session, film, offers=[(FANDANGO, "buy")])
    await session.commit()
    await prune(session, apply=True)

    await _digital(session, film, today - timedelta(days=1))
    await session.commit()
    respx.get(f"{BASE_URL}/movie/9604/watch/providers").mock(
        return_value=httpx.Response(
            200, json=make_watch_providers(9604, buy=[FANDANGO], rent=[APPLE])
        )
    )
    run_id = await runs.create_run(session, kind="providers")
    await session.commit()
    async with TMDBClient(
        base_url=BASE_URL,
        api_key="test-key",
        rate_calls=100,
        rate_window=1,
        retry_max_attempts=2,
        retry_base_delay=0.01,
    ) as client:
        now = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=6)
        result = await run_provider_poll(
            session_factory=session_factory,
            client=client,
            run_id=run_id,
            today=today,
            min_age_days=14,
            max_age_days=365,
            now=now,
        )

    assert (result.held, result.cards) == (0, 1)
    (card,) = (
        (
            await session.execute(
                select(Event).where(Event.film_id == film.id, Event.event_type == "now_available")
            )
        )
        .scalars()
        .all()
    )
    assert card.occurred_at == now
    assert card.subject_key == ["US:rent", "US:buy"]


async def test_a_later_uncarded_preorder_row_goes_too(session):
    """A second store listing the pre-order a day later inserted a `buy` row and carded
    nothing, `buy` being known already. Left in place, the real release would still find `buy`
    known and card nothing (D-1538.5) — so every row the gate would have held goes, not only
    the card's own observation. A row observed on or after the date stays."""
    film = await add_film(session, 9605, status="Released")
    digital = OBSERVED.date() + timedelta(days=10)
    await _digital(session, film, digital)
    await _card(session, film, offers=[(FANDANGO, "buy")])
    await _ledger_row(session, film, APPLE, "buy", OBSERVED + timedelta(days=1))
    after = datetime.combine(digital, datetime.min.time(), tzinfo=UTC) + timedelta(hours=6)
    await _ledger_row(session, film, 8, "flatrate", after)
    await session.commit()

    pruned = await prune(session, apply=True)

    assert [(row.provider, row.monetization_type) for row in pruned.ledger] == [
        ("Provider 7", "buy"),
        ("Provider 2", "buy"),
    ]
    assert pruned.deleted["availability_first_seen"] == 2
    assert await _ledger_providers(session, film) == [8]
