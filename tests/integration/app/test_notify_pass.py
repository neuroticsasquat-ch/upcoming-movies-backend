"""The M7 decision pass (NEU-1379): who gets an alert, who gets a digest line, and who gets a
`suppressed` row instead of mail.

Every test here seeds a watermark by hand — a `succeeded` notify run at a fixed instant — and
dates its events either side of it, so the window is explicit rather than a function of when
the suite happened to run. `run_notify_pass` reads that watermark exactly as production does.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update

from upmovies.app.models import (
    Follow,
    Notification,
    PushSubscription,
    UserSettings,
)
from upmovies.app.services.notify_service import (
    NotifyResult,
    notify_detail,
    run_notify_pass,
)
from upmovies.catalog.models import (
    FilmCompanyChange,
    FilmCreditChange,
    FilmProductionCompany,
    Person,
    ProductionCompany,
)
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run, finalize_run
from upmovies.ingest.sweep import confirm_stamped_cards
from upmovies.news.models import Event, EventStory, Story, StoryEntity, StoryPerson
from upmovies.news.subject_key import (
    COLLECTION_SUBJECT_PREFIX,
    COMPANY_SUBJECT_PREFIX,
    normalize_name,
)

WATERMARK = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)
"""When the last successful notify run began. Events created after it are this pass's work."""
NEW = datetime(2026, 9, 18, 3, 0, tzinfo=UTC)
OLD = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
BETWEEN = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
"""Where the watermark lands after a first pass — between the two windows below."""
LATER = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
TODAY = date(2026, 9, 18)
"""Only a fixture date now. The pass itself takes no `today`, no excluded statuses and no age
bound since M3: both branches read the timeline's own clause, and neither half of it has a
window to bound (EF-3, D-1437.7)."""
GRANTED = datetime(2027, 1, 1, tzinfo=UTC)
LAPSED = datetime(2026, 1, 1, tzinfo=UTC)
VERIFIED = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def seed_watermark(session):
    async def _seed(at: datetime = WATERMARK) -> None:
        session.add(IngestRun(kind="notify", status="succeeded", started_at=at, finished_at=at))
        await session.commit()

    return _seed


@pytest.fixture
def move_watermark(session):
    """Re-date every notify run to `at`, so a second pass's window is a fact of the fixture
    rather than of the wall clock the first run stamped itself with."""

    async def _move(at: datetime) -> None:
        await session.execute(
            update(IngestRun).where(IngestRun.kind == "notify").values(started_at=at)
        )
        await session.commit()

    return _move


@pytest.fixture
def run_pass(session_factory):
    """Run the pass the way `pipeline_run.run_notify_stage` does — its own run row, its own
    sessions — so the watermark this run leaves behind is the real one."""

    async def _run() -> NotifyResult:
        async with session_factory() as s:
            run_id = await create_run(s, kind="notify")
            await s.commit()
        result = await run_notify_pass(session_factory=session_factory, run_id=run_id)
        async with session_factory() as s:
            await finalize_run(s, run_id, status="failed" if result.aborted else "succeeded")
            await s.commit()
        return result

    return _run


@pytest.fixture
def subscriber(make_user):
    """The ordinary recipient: verified and holding a live grant."""

    async def _make(email: str = "sub@example.com", **kwargs):
        kwargs.setdefault("entitled_until", GRANTED)
        kwargs.setdefault("email_verified_at", VERIFIED)
        return await make_user(email=email, **kwargs)

    return _make


async def _rows(session) -> list[Notification]:
    return list(
        (
            await session.execute(
                select(Notification).order_by(Notification.kind, Notification.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _follow_title(session, *, user_id: UUID, film_id: UUID) -> None:
    """Follow a film by title — which, since M8, is what puts it on the watchlist *and* on the
    timeline. There is no way to have one without the other, and that is the model: a follow is
    the only thing a user keeps (D-42)."""
    session.add(
        Follow(user_id=user_id, entity_type="title", entity_id=str(film_id), source="manual")
    )
    await session.commit()


async def _follow_person(session, *, user_id: UUID, person_id: int) -> None:
    """Follow a person — which, since M3, puts that person's *attachment cards* on this user's
    timeline and digest, and nothing else about the films they are on (EF-3)."""
    session.add(
        Follow(
            user_id=user_id,
            entity_type="person",
            entity_id=str(person_id),
            source="manual",
        )
    )
    await session.commit()


async def _attach_card(add_event, film, *, person_name: str, event_type: str = "casting", **kw):
    """A catalog credit card as the sweep writes one: `rumored` until quarantine clears, with
    the person's normalized name in `subject_key` and nothing else identifying them."""
    kw.setdefault("provenance", "catalog")
    kw.setdefault("confidence", "rumored")
    return await add_event(
        film=film,
        event_type=event_type,
        subject_key=[normalize_name(person_name)],
        **kw,
    )


async def _set_alert_stores(session, *, user_id: UUID, alert_stores: list[str]) -> None:
    session.add(
        UserSettings(
            user_id=user_id,
            alert_stores=alert_stores,
            ical_token=f"tok-{user_id}",
            unsubscribe_token=f"unsub-{user_id}",
        )
    )
    await session.commit()


async def test_a_title_follow_on_a_whitelist_beat_queues_an_alert_and_a_digest(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """Both branches from one row, which is EF-14's whole shape: the film is on this user's
    calendar *and* on their timeline because they follow it, and an alert and a digest line are
    different deliveries of the same news (`app.models.Notification`)."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    event = await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued, result.suppressed) == (1, 1, 0)
    alert, digest = await _rows(session)
    assert (alert.user_id, alert.event_id) == (user.id, event.id)
    assert (alert.kind, alert.channel, alert.status) == ("alert", "email", "queued")
    assert alert.sent_at is None
    assert (digest.kind, digest.event_id) == ("digest", event.id)


async def test_a_followed_persons_attachment_card_alerts_and_lines_the_digest(
    session,
    session_factory,
    subscriber,
    make_user,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
):
    """What an entity follow earns from this pass, in full (EF-3, EF-7, EF-8): the attachment
    card, as an alert *and* as a digest line.

    The card is `rumored`, which is what every catalog attachment is until its quarantine
    clears. Under the interim rule (D-1437.7) that made it digest-only; EF-8 is that surviving
    quarantine *is* the confirmation for a `provenance = catalog` card, so this is the row the
    follow exists to deliver and it is allowed to interrupt. The non-follower is the control:
    one card, one recipient."""
    await seed_watermark()
    user = await subscriber()
    stranger = await subscriber(email="stranger@example.com")
    film = await make_film(slug="dune", title="Dune")
    await make_person(id=488, name="A Director")
    await _follow_person(session, user_id=user.id, person_id=488)
    await _follow_title(
        session,
        user_id=stranger.id,
        film_id=(await make_film(slug="something-else", title="Something Else")).id,
    )
    card = await _attach_card(
        add_event, film, person_name="A Director", event_type="crew_attached", created_at=NEW
    )

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued, result.push_alerts_queued) == (1, 1, 0)
    assert {(row.user_id, row.event_id, row.kind) for row in await _rows(session)} == {
        (user.id, card.id, "alert"),
        (user.id, card.id, "digest"),
    }


async def test_a_person_follow_no_longer_reaches_the_films_own_beats(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """The cutover's consequence for this pass (EF-3). Under D-11 a credit put the whole film on
    the user's timeline, so a release-date change earned them an alert *and* a digest line; now
    a person follow delivers attachments and the film's own beats belong to its own followers.

    Every credit is checked in `test_follow_queries.py`; what is pinned here is that the branch
    reading the film is the *title* branch and a person follow does not reach it."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune", release_date=date(2099, 1, 1))
    await attach_credits(film, crew=[{"id": 488, "name": "A Writer", "job": "Screenplay"}])
    await _follow_person(session, user_id=user.id, person_id=488)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 0)
    assert await _rows(session) == []


@pytest.mark.parametrize(
    ("event_type", "confidence"),
    [
        pytest.param("casting", "rumored", id="a casting attachment"),
        pytest.param("crew_attached", "rumored", id="a crew attachment"),
        pytest.param("credit_removed", "rumored", id="a detachment"),
        pytest.param("canceled", "confirmed", id="a cancellation"),
    ],
)
async def test_an_entity_follower_is_alerted_by_every_card_their_follow_delivers(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
    event_type,
    confidence,
):
    """EF-7's entity arm, against the four cards that reach it (the studio and franchise pair
    are `test_a_company_follower_is_alerted_by_the_studio_joining` below).

    `ENTITY_PUSH_TYPES` is the vocabulary of `entity_attachment_event_ids` rather than a
    narrowing of it, so the interesting assertion is that *nothing* an entity follow delivers is
    digest-only. The three attach and detach cards are `rumored` catalog cards and push on
    EF-8's provenance rule; `canceled` is confirmed and pushes on the ordinary floor."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    # Credited as well as named, so the `canceled` case has an attachment to be found through.
    # `attach_credits` writes the person row, which is why this test does not also `make_person`.
    await attach_credits(film, crew=[{"id": 488, "name": "A Director", "job": "Director"}])
    await _follow_person(session, user_id=user.id, person_id=488)
    await _attach_card(
        add_event,
        film,
        person_name="A Director",
        event_type=event_type,
        confidence=confidence,
        created_at=NEW,
    )

    result = await run_pass()

    assert (result.alerts_queued, result.push_alerts_queued) == (1, 0)
    assert sorted(row.kind for row in await _rows(session)) == ["alert", "digest"]


@pytest.mark.parametrize(
    "event_type",
    ["company_attached", "company_removed", "collection_attached", "collection_removed"],
)
async def test_a_company_follower_is_alerted_by_the_studio_joining(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    event_type,
):
    """The organisation half of the entity arm (EF-7). These four types are in
    `ENTITY_PUSH_TYPES` and in no title arm at all, which is the asymmetry the split exists
    for — see `test_a_title_follow_is_not_interrupted_by_a_studio_joining`."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    entity_type = "company" if event_type.startswith("company") else "franchise"
    prefix = COMPANY_SUBJECT_PREFIX if entity_type == "company" else COLLECTION_SUBJECT_PREFIX
    session.add(Follow(user_id=user.id, entity_type=entity_type, entity_id="33", source="manual"))
    await session.commit()
    await add_event(
        film=film,
        event_type=event_type,
        provenance="catalog",
        confidence="rumored",
        subject_key=[f"{prefix}33"],
        created_at=NEW,
    )

    result = await run_pass()

    assert result.alerts_queued == 1
    assert sorted(row.kind for row in await _rows(session)) == ["alert", "digest"]


async def test_a_title_follow_is_not_interrupted_by_a_studio_joining(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The other side of that asymmetry (EF-7). Which company financed a film you follow is
    timeline news; it is the *studio's* followers it interrupts."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(
        film=film,
        event_type="company_attached",
        provenance="catalog",
        confidence="rumored",
        subject_key=[f"{COMPANY_SUBJECT_PREFIX}33"],
        created_at=NEW,
    )

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 1)


async def test_unfollowing_the_film_earns_neither_kind(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """EF-14: the mute that used to silence a film without touching its follow is gone with the
    watchlist it corrected, so the only way a followed film stops earning deliveries is the
    follow itself going. Both branches drop together, as they did under the mute."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)
    await session.execute(sa_delete(Follow).where(Follow.user_id == user.id))
    await session.commit()

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 0)
    assert await _rows(session) == []


async def test_a_beat_in_neither_push_set_queues_no_alert(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """EF-7 replaced D-32's closed list with two sets, but it did not open the vocabulary: a
    type in neither arm is digest material however the card reached the user."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="production_start", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 1)
    assert [row.kind for row in await _rows(session)] == ["digest"]


async def test_a_title_follow_is_alerted_by_a_seed_grade_casting_card(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """EF-9's admitting side. The performer is 2nd billed, so the card names a seed-grade role
    on the film and is worth interrupting the film's follower about."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await attach_credits(film, cast=[{"id": 91, "name": "A Lead", "credit_order": 1}])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await _attach_card(add_event, film, person_name="A Lead", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (1, 1)


async def test_a_title_follow_is_not_alerted_by_a_twelfth_billed_addition(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """EF-9's whole point: the same card, the same follow, one billing position apart from the
    test above. A 12th-billed addition reaches the timeline and the digest, not a lock screen.

    The performer's *own* follower is not in this test — that reach has no seed-grade floor,
    and `test_an_entity_follower_is_alerted_by_every_card_their_follow_delivers` covers it."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await attach_credits(film, cast=[{"id": 92, "name": "A Bit Part", "credit_order": 11}])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await _attach_card(add_event, film, person_name="A Bit Part", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 1)


async def test_a_title_follow_is_not_alerted_by_a_non_seed_crew_addition(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """The crew half of the same floor. A gaffer joining cards as `crew_attached` beside a
    director (`CREDIT_ROLE_EVENT_TYPES`), so the event type cannot tell them apart and the
    grade has to be read off the credit."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await attach_credits(film, crew=[{"id": 93, "name": "A Gaffer", "job": "Gaffer"}])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await _attach_card(
        add_event, film, person_name="A Gaffer", event_type="crew_attached", created_at=NEW
    )

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 1)


async def test_a_title_follow_is_alerted_by_a_directors_departure(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
):
    """A detachment is graded off `catalog.film_credit_change`, not off `film_credit`: that
    table is delete-and-rebuilt every ingest, so the credit this card is *about* is gone from it
    by the time the pass runs (`names_a_seed_grade_credit`). The history is what remembers the
    job, so a director leaving still interrupts the film's follower."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    session.add(Person(id=94, name="A Director"))
    await session.flush()
    session.add(
        FilmCreditChange(
            film_id=film.id,
            person_id=94,
            credit_type="crew",
            job="Director",
            change="removed",
            changed_at=NEW,
        )
    )
    await session.commit()
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await _attach_card(
        add_event, film, person_name="A Director", event_type="credit_removed", created_at=NEW
    )

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (1, 1)


async def test_now_available_alerts_only_the_stores_the_user_wants(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """D-44: the availability beat is the one whitelist entry a user's setting narrows, and it
    is one setting for everything they follow now. The streamer holds no settings row at all —
    `{stream}` is the default the pass COALESCEs to — and the card spells that type
    `flatrate`."""
    await seed_watermark()
    streamer = await subscriber(email="streamer@example.com")
    buyer = await subscriber(email="buyer@example.com")
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=streamer.id, film_id=film.id)
    await _follow_title(session, user_id=buyer.id, film_id=film.id)
    await _set_alert_stores(session, user_id=buyer.id, alert_stores=["buy"])
    await add_event(
        film=film,
        event_type="now_available",
        provenance="catalog",
        region="US",
        subject_key=["US:flatrate"],
        created_at=NEW,
    )

    result = await run_pass()

    assert result.alerts_queued == 1
    (alert,) = [row for row in await _rows(session) if row.kind == "alert"]
    assert alert.user_id == streamer.id


async def test_a_director_follow_no_longer_alerts_on_a_films_now_available_beat(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """The alert NEU-1417 widened the window for, deliberately retired (EF-3, EF-7).

    The user follows nobody but the director; the film opened two months ago and its streaming
    debut has just been observed. D-46 made that alert reachable by stretching the alert window
    past release day — ADR-0019 removes the premise instead: a person follow delivers that
    person's attachments, and where their films end up streaming is the *film's* news, for
    whoever followed the film. The title follower beside them still gets it, which is the
    replacement path and what the film page's one button now offers.

    Not an interim state and not NEU-1438's to flip: EF-7's entity push set is the attachment
    beats and the cancellation, and `now_available` is not in it by design."""
    await seed_watermark()
    indirect = await subscriber(email="director-follower@example.com")
    direct = await subscriber(email="film-follower@example.com")
    film = await make_film(
        slug="dune", title="Dune", release_date=TODAY - timedelta(days=60), status="Released"
    )
    await attach_credits(film, crew=[{"id": 525, "name": "A Director", "job": "Director"}])
    await _follow_person(session, user_id=indirect.id, person_id=525)
    await _follow_title(session, user_id=direct.id, film_id=film.id)
    await add_event(
        film=film,
        event_type="now_available",
        provenance="catalog",
        region="US",
        subject_key=["US:flatrate"],
        created_at=NEW,
    )

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (1, 1)
    assert {(row.user_id, row.kind) for row in await _rows(session)} == {
        (direct.id, "alert"),
        (direct.id, "digest"),
    }


async def test_a_rumored_card_is_queued_as_a_digest_and_not_as_an_alert(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The floor that moved (D-1437.7). It used to sit in `deliverable_events`, so a `rumored`
    card was queued in no kind at all — which, once every catalog attachment arrived `rumored`,
    would have left an entity follower's digest empty of the only thing their follow delivers.

    EF-7: the digest carries everything the timeline carries, and confirmation is what a *push*
    waits for. So this card is a digest line and not an alert, on a whitelist beat, through a
    title follow — every other reason to decline it removed, so what the assertion reads is the
    confidence term and nothing else."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    event = await add_event(
        film=film, event_type="release_date", confidence="rumored", created_at=NEW
    )

    result = await run_pass()

    assert result.events_considered == 1, "the shared selector no longer cuts on confidence"
    assert (result.alerts_queued, result.digests_queued) == (0, 1)
    (row,) = await _rows(session)
    assert (row.kind, row.event_id) == ("digest", event.id)


async def test_a_confirmed_card_on_the_whitelist_still_alerts(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The other side of the same pair, so the floor is pinned as *moved* rather than dropped."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (1, 1)


async def test_a_superseded_event_is_not_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """A superseded card is still *rendered* everywhere (ADR-0017), but the correction that
    replaced it is the one worth mailing about."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", status="superseded", created_at=NEW)

    assert (await run_pass()).alerts_queued == 0
    assert await _rows(session) == []


async def test_events_published_before_the_watermark_are_left_alone(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The window is the publication axis (ADR-0016) — anything the previous run could have
    seen is its business, not this one's."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=OLD)
    await add_event(film=film, event_type="trailer", created_at=NEW)

    result = await run_pass()

    assert result.events_considered == 1
    assert [row.kind for row in await _rows(session)] == ["alert", "digest"]


async def test_re_deciding_the_same_window_writes_nothing_twice(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """The idempotence that matters: a run that failed leaves its window undecided, so the next
    one re-reads it in full. `uq_notification_user_event_kind_channel` is what makes that free.

    The second pass here sees the *same* watermark, because the first run's own `succeeded` row
    is backdated to it — exactly the overlap a crash produces."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    first = await run_pass()
    await move_watermark(WATERMARK)
    second = await run_pass()

    assert (first.alerts_queued, first.digests_queued) == (1, 1)
    assert (second.alerts_queued, second.digests_queued) == (0, 0)
    assert second.events_considered == 1, "the window really was re-read"
    assert len(await _rows(session)) == 2


async def test_a_rumored_story_attachment_waits_and_pushes_on_its_upgrade(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """EF-10 end to end, and the reason the alert branch reads a widened window.

    A trade says the director is "in talks": a story-backed `rumored` attach card. It lines the
    digest and interrupts nobody. Days later the association confirms and the card is upgraded
    **in place** (D-6) rather than re-carded, so its `created_at` is now behind the watermark
    and only `updated_at` moved. The upgrade is what queues the push — and it queues exactly
    one, because the second pass's digest row is the first pass's row and the unique key
    declines it."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await make_person(id=488, name="A Director")
    await _follow_person(session, user_id=user.id, person_id=488)
    card = await add_event(
        film=film,
        event_type="crew_attached",
        provenance="story",
        confidence="rumored",
        subject_key=[normalize_name("A Director")],
        created_at=NEW,
    )

    waiting = await run_pass()

    assert (waiting.alerts_queued, waiting.digests_queued) == (0, 1)

    await move_watermark(BETWEEN)
    await session.execute(
        update(Event).where(Event.id == card.id).values(confidence="confirmed", updated_at=LATER)
    )
    await session.commit()

    upgraded = await run_pass()

    assert (upgraded.alerts_queued, upgraded.digests_queued) == (1, 0)
    assert upgraded.events_considered == 0, (
        "the counter stays on publication; only the alert branch reopens the window"
    )
    assert sorted(row.kind for row in await _rows(session)) == ["alert", "digest"]


async def test_an_upgrade_that_never_comes_never_pushes(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """The control for the test above. The card is touched in the later window — a second
    outlet attaching to it bumps `updated_at` (`link.cluster`) — but it is still "in talks", so
    re-considering it must change nothing. Without this, the widened window would read as
    "anything edited recently pushes"."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await make_person(id=488, name="A Director")
    await _follow_person(session, user_id=user.id, person_id=488)
    card = await add_event(
        film=film,
        event_type="crew_attached",
        provenance="story",
        confidence="rumored",
        subject_key=[normalize_name("A Director")],
        created_at=NEW,
    )

    await run_pass()
    await move_watermark(BETWEEN)
    await session.execute(update(Event).where(Event.id == card.id).values(updated_at=LATER))
    await session.commit()

    second = await run_pass()

    assert (second.alerts_queued, second.digests_queued) == (0, 0)
    assert [row.kind for row in await _rows(session)] == ["digest"]


async def test_a_catalog_attachment_touched_later_is_not_reconsidered(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """The bound on the reopened window. A second outlet clustering onto an existing card bumps
    its `updated_at` and changes nothing else (`link.cluster`), and catalog cards were pushable
    the day they published (EF-8) — so reopening on `updated_at` alone would push an attachment
    of any age at a follower who arrived after it.

    Here the follow is created *after* the first pass, so the card has alerted nobody and the
    unique key cannot mask the difference: if the arm admitted this row the follower would be
    interrupted about last window's news."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await make_person(id=488, name="A Director")
    card = await _attach_card(
        add_event, film, person_name="A Director", event_type="crew_attached", created_at=NEW
    )

    await run_pass()
    await move_watermark(BETWEEN)
    await _follow_person(session, user_id=user.id, person_id=488)
    await session.execute(update(Event).where(Event.id == card.id).values(updated_at=LATER))
    await session.commit()

    second = await run_pass()

    assert (second.alerts_queued, second.digests_queued) == (0, 0)
    assert await _rows(session) == []


async def test_an_unrelated_casting_card_is_not_graded_by_an_old_writing_credit(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """The `film_credit_change` arm of the seed-grade floor is a fallback for a credit that is
    *gone*, so it answers `credit_removed` and nothing else.

    This performer is 12th billed now and once held a writing credit on the same film. On an
    attachment the live row is present and authoritative: the card is about the bit part, and
    grading it off the old writing credit would pass exactly the addition EF-9 excludes."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await attach_credits(film, cast=[{"id": 95, "name": "A Bit Part", "credit_order": 11}])
    session.add(
        FilmCreditChange(
            film_id=film.id,
            person_id=95,
            credit_type="crew",
            job="Screenplay",
            change="removed",
            changed_at=OLD,
        )
    )
    await session.commit()
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await _attach_card(add_event, film, person_name="A Bit Part", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 1)


async def test_a_card_both_follows_reach_earns_one_row_per_channel_and_no_more(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """EF-7's closing clause. The user follows the film *and* its director, so the attachment
    card reaches them twice over — through `title_follow_film_ids` and through
    `entity_attachment_event_ids`. Both arms admit it, and they are arms of one decision: the
    branch produces event **ids**, so the reach that admitted the card leaves no trace in what
    is written."""
    await seed_watermark()
    user = await subscriber()
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await attach_credits(film, crew=[{"id": 488, "name": "A Director", "job": "Director"}])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await _follow_person(session, user_id=user.id, person_id=488)
    card = await _attach_card(
        add_event, film, person_name="A Director", event_type="crew_attached", created_at=NEW
    )

    result = await run_pass()

    assert (result.alerts_queued, result.push_alerts_queued, result.digests_queued) == (1, 1, 1)
    assert sorted((row.kind, row.channel) for row in await _rows(session)) == [
        ("alert", "email"),
        ("alert", "push"),
        ("digest", "email"),
    ]
    assert {row.event_id for row in await _rows(session)} == {card.id}


async def test_the_first_run_ever_establishes_the_watermark_and_queues_nothing(
    session, session_factory, subscriber, make_film, add_event, run_pass
):
    """No `seed_watermark` here. "Everything since the beginning of time" is the whole ledger,
    so a cold start deliberately mails nobody rather than a year of news at once."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert result.cold_start
    assert await _rows(session) == []


async def test_an_unentitled_user_is_suppressed_on_both_branches(
    session, session_factory, make_user, make_film, add_event, seed_watermark, run_pass
):
    """D-39's batch checkpoint. A row, not an absence: "we decided not to mail you" has to be
    distinguishable from "nobody considered you"."""
    await seed_watermark()
    user = await make_user(email="lapsed@example.com", email_verified_at=VERIFIED)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued, result.suppressed) == (0, 0, 2)
    rows = await _rows(session)
    assert [row.kind for row in rows] == ["alert", "digest"]
    assert {row.status for row in rows} == {"suppressed"}


async def test_an_unverified_user_is_suppressed(
    session, session_factory, make_user, make_film, add_event, seed_watermark, run_pass
):
    """D-31's half of the same decision — an entitled subscriber who never confirmed their
    address. Verification gates outbound mail and nothing else, so they keep the app."""
    await seed_watermark()
    user = await make_user(email="unconfirmed@example.com", entitled_until=GRANTED)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued, result.suppressed) == (0, 0, 2)
    assert {row.status for row in await _rows(session)} == {"suppressed"}


async def test_a_grant_that_lapses_between_runs_suppresses_the_next_window(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """The case a single-run test cannot show: entitlement is read per run, not per account, so
    the same user is queued on one pass and suppressed on the next. The first window's row is
    left exactly as it was — revoking suppresses, it never destroys (D-40)."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    first_event = await add_event(film=film, event_type="release_date", created_at=NEW)

    assert (await run_pass()).alerts_queued == 1
    await move_watermark(BETWEEN)

    user.entitled_until = LAPSED
    await session.commit()
    second_event = await add_event(film=film, event_type="trailer", created_at=LATER)

    result = await run_pass()

    assert (result.alerts_queued, result.suppressed) == (0, 2)
    alerts = {row.event_id: row for row in await _rows(session) if row.kind == "alert"}
    assert alerts[first_event.id].status == "queued"
    assert alerts[second_event.id].status == "suppressed"


async def test_a_story_that_merely_names_a_followed_person_is_not_in_their_digest(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
):
    """The negative of what this test used to assert, and the heart of EF-13.

    Under D-11 any event whose story named a resolved followed person was theirs — so a date
    change reported in an article that mentions the director was a digest line about a film
    they may have nothing to do with. Now a mention reaches them only as the person's *first
    association* with the film, and a `release_date` card is not an attach card at all, so
    `first_association_clause` declines it on its first term."""
    await seed_watermark()
    user = await subscriber()
    person = await make_person(id=525, name="A Director")
    film = await make_film(slug="uncredited", title="Uncredited")
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id=str(person.id), source="manual")
    )
    await session.commit()
    await add_event(
        film=film,
        event_type="release_date",
        created_at=NEW,
        sources=({"url": "https://deadline.example/story"},),
    )
    story_id = (
        await session.execute(select(Story.id).where(Story.url == "https://deadline.example/story"))
    ).scalar_one()
    session.add(
        StoryPerson(
            story_id=story_id,
            person_id=person.id,
            name_as_written="A Director",
            path="accepted",
            features={"title_mentioned": None, "event_type": "casting"},
            prompt_version="1",
        )
    )
    await session.commit()

    result = await run_pass()

    assert result.digests_queued == 0
    assert await _rows(session) == []


async def test_a_first_association_is_in_the_digest(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
):
    """The other side: the same mention on a `casting` card, for somebody holding no credit on
    the film, is the first-association beat — and it is digest material, `rumored` and all."""
    await seed_watermark()
    user = await subscriber()
    person = await make_person(id=525, name="A Director")
    film = await make_film(slug="uncredited", title="Uncredited")
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id=str(person.id), source="manual")
    )
    await session.commit()
    event = await add_event(
        film=film,
        event_type="casting",
        confidence="rumored",
        created_at=NEW,
        sources=({"url": "https://deadline.example/scoop"},),
    )
    story_id = (
        await session.execute(select(Story.id).where(Story.url == "https://deadline.example/scoop"))
    ).scalar_one()
    session.add(
        StoryPerson(
            story_id=story_id,
            person_id=person.id,
            name_as_written="A Director",
            path="accepted",
            features={"title_mentioned": None, "event_type": "casting"},
            prompt_version="1",
        )
    )
    await session.commit()

    result = await run_pass()

    assert (result.digests_queued, result.alerts_queued) == (1, 0)
    (row,) = await _rows(session)
    assert (row.event_id, row.kind) == (event.id, "digest")


async def test_both_branches_reach_the_one_first_association_builder(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    monkeypatch,
):
    """The contract NEU-1446 inherits: EF-13's predicate is **one** builder, and both branches
    of this pass reach it through `entity_attachment_event_ids` rather than either of them
    spelling the rule again. M4 extends the builder to `story_entity`; if a second copy of the
    predicate had grown here, M4 would widen one branch and quietly leave the other behind.

    Asserted by counting calls rather than by comparing SQL: what matters is that the alert
    branch and the digest branch each go through it, once, for the user being decided for."""
    from upmovies.app import follow_queries

    calls: list[dict] = []
    real = follow_queries.first_association_clause

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(follow_queries, "first_association_clause", spy)

    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (1, 1)
    assert len(calls) == 2, "once from the alert branch and once from the digest branch"
    assert all(call["user_id"] == user.id for call in calls)


async def test_a_user_with_no_graph_is_never_considered(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The pass is proportional to the follow graph, not to signups: an account that follows
    nothing is owed no decision, so it buys no statements."""
    await seed_watermark()
    await subscriber(email="lurker@example.com")
    film = await make_film(slug="dune", title="Dune")
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert result.users_considered == 0
    assert await _rows(session) == []


async def test_a_hidden_event_type_is_never_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """`other` is the uncategorized catch-all, hidden from every surface (`news.visibility`).
    A mail about a card the product will not show is worse than no mail."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="other", created_at=NEW)

    result = await run_pass()

    assert result.events_considered == 0
    assert await _rows(session) == []


async def test_an_event_with_no_summary_is_never_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The sender's precondition: a notification with no summary behind it is a mail with
    nothing to say."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="casting", summary=None, created_at=NEW)

    assert (await run_pass()).digests_queued == 0
    assert await _rows(session) == []


async def test_a_release_date_outside_the_films_visible_markets_is_never_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """D-32 is "US theatrical or home-release". An Indian date change on a US film is hidden
    from the feed and the film page by `region_visible()`, so alerting on it would mail news
    the product then refuses to show."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune", origin_country=["US"])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(
        film=film, event_type="release_date", provenance="catalog", region="IN", created_at=NEW
    )

    result = await run_pass()

    assert result.alerts_queued == 0
    assert await _rows(session) == []


async def test_a_release_date_in_the_films_origin_country_is_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The other side of the same rule — the film's own market is visible, so it alerts."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="rrr", title="RRR", origin_country=["IN"])
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(
        film=film, event_type="release_date", provenance="catalog", region="IN", created_at=NEW
    )

    assert (await run_pass()).alerts_queued == 1


async def test_a_film_with_no_slug_is_never_queued(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """No slug, no page to link the mail at."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    film.slug = None
    await session.commit()
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="trailer", created_at=NEW)

    assert (await run_pass()).alerts_queued == 0
    assert await _rows(session) == []


# --- the push channel (D-36) ----------------------------------------------------------------


async def _register_push(session, *, user_id: UUID, endpoint: str = "https://push.test/ep") -> None:
    session.add(PushSubscription(user_id=user_id, endpoint=endpoint, p256dh="key", auth="secret"))
    await session.commit()


def _by_channel(rows: list[Notification]) -> dict[str, Notification]:
    return {row.channel: row for row in rows}


async def test_a_registered_browser_earns_a_push_row_beside_the_mail(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """D-36's whole contract on this side: the same event, the same `alert` kind, twice — once
    per channel. Not a second decision, so the push row carries the same status."""
    await seed_watermark()
    user = await subscriber()
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    event = await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.push_alerts_queued, result.digests_queued) == (1, 1, 1)
    alerts = _by_channel([row for row in await _rows(session) if row.kind == "alert"])
    assert set(alerts) == {"email", "push"}
    for row in alerts.values():
        assert (row.event_id, row.kind, row.status) == (event.id, "alert", "queued")


async def test_a_user_with_no_subscription_gets_no_push_row(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The one that must not regress: a `push` row for a user with nowhere to send it would sit
    in the backlog being retried every night."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="trailer", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.push_alerts_queued) == (1, 0)
    assert [(row.kind, row.channel) for row in await _rows(session)] == [
        ("alert", "email"),
        ("digest", "email"),
    ]


async def test_a_digest_is_never_queued_by_push(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """A follow produces timeline material, and the timeline is a mail. Only the alert branch
    has a push half.

    Asserted on a beat that *does* push, so the absence read here is the digest row's missing
    twin rather than a card nothing would have pushed anyway."""
    await seed_watermark()
    user = await subscriber()
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.digests_queued, result.push_alerts_queued) == (1, 1)
    assert {(row.kind, row.channel) for row in await _rows(session)} == {
        ("alert", "email"),
        ("alert", "push"),
        ("digest", "email"),
    }


async def test_a_beat_in_neither_push_set_queues_no_push_either(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The push branch inherits the alert branch's decision rather than restating it."""
    await seed_watermark()
    user = await subscriber()
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="production_start", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.push_alerts_queued) == (0, 0)
    assert [(row.kind, row.channel) for row in await _rows(session)] == [("digest", "email")]


async def test_an_unentitled_subscriber_is_suppressed_on_the_push_row_too(
    session, session_factory, make_user, make_film, add_event, seed_watermark, run_pass
):
    """D-39 against D-40: the lapsed subscriber keeps their registration, so the push row is
    written — and written `suppressed`, so the browser hears nothing and the decision is on the
    record."""
    await seed_watermark()
    user = await make_user(email="lapsed@example.com", email_verified_at=VERIFIED)
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.push_alerts_queued, result.suppressed) == (0, 0, 3)
    alerts = _by_channel([row for row in await _rows(session) if row.kind == "alert"])
    assert set(alerts) == {"email", "push"}
    assert {row.status for row in await _rows(session)} == {"suppressed"}
    # And the registration survived the pass (D-40).
    assert (await session.execute(select(PushSubscription))).scalars().all() != []


async def test_a_second_pass_over_the_same_window_re_queues_no_push_row(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """`channel` is in the unique key, so the push row de-duplicates on the same terms the mail
    does."""
    await seed_watermark()
    user = await subscriber()
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    first = await run_pass()
    await move_watermark(WATERMARK)
    second = await run_pass()

    assert (first.alerts_queued, first.push_alerts_queued) == (1, 1)
    assert (second.alerts_queued, second.push_alerts_queued) == (0, 0)
    assert len(await _rows(session)) == 3


async def test_the_detail_line_reports_the_push_count(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    await seed_watermark()
    user = await subscriber()
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    assert "1 alerts, 1 push" in notify_detail(await run_pass())


async def test_a_studio_scoop_lines_the_digest_then_pushes_once_when_the_catalog_confirms(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    seed_watermark,
    run_pass,
    move_watermark,
):
    """EF-10 end to end for a studio, with the flip performed by its real writer.

    `test_a_rumored_card_pushes_when_it_is_upgraded` pins the same shape with the upgrade done
    by hand, because when it was written nothing performed one. This is that test closed: the
    story card, the stamped change, `confirm_stamped_cards` at the end of the sweep, and one
    push — the second pass's digest row is the first pass's row, and the unique key declines
    it."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    session.add(ProductionCompany(id=3172, name="Blumhouse"))
    session.add(Follow(user_id=user.id, entity_type="company", entity_id="3172", source="manual"))
    await session.commit()

    # The card as the cluster stage published it: story provenance, `rumored`, and no
    # organisation token — resolution runs later, which is why the follow reaches it through
    # `first_association_clause` rather than through the token branch.
    card = await add_event(
        film=film,
        event_type="company_attached",
        provenance="story",
        confidence="rumored",
        created_at=NEW,
        sources=({"url": "https://deadline.test/scoop"},),
    )
    story_id = await session.scalar(
        select(EventStory.story_id).where(EventStory.event_id == card.id)
    )
    session.add(
        StoryEntity(
            story_id=story_id,
            kind="company",
            entity_id=3172,
            name_as_written="Blumhouse",
            path="accepted",
            features={"title_mentioned": None, "event_type": "company_attached"},
            prompt_version="1",
        )
    )
    await session.commit()

    waiting = await run_pass()

    assert (waiting.alerts_queued, waiting.digests_queued) == (0, 1), (
        "a rumored story card is digest-only until the catalog confirms it (EF-10)"
    )

    # TMDB observes the attachment; the sweep's backward pass stamps it with the card that
    # published it, and the confirmation phase flips the card once the window has passed.
    session.add(FilmProductionCompany(film_id=film.id, company_id=3172))
    session.add(
        FilmCompanyChange(
            film_id=film.id,
            company_id=3172,
            change="added",
            changed_at=LATER - timedelta(days=4),
            carded_by_event_id=card.id,
        )
    )
    await session.commit()
    await move_watermark(BETWEEN)
    assert await confirm_stamped_cards(session, now=LATER, quarantine=timedelta(hours=72)) == (
        1,
        1,
        0,
    )
    await session.commit()

    upgraded = await run_pass()

    assert (upgraded.alerts_queued, upgraded.digests_queued) == (1, 0)
    assert sorted(row.kind for row in await _rows(session)) == ["alert", "digest"]

    # And only once: a third pass over an already-confirmed card selects nothing to flip, so
    # `updated_at` does not move again and the window does not reopen.
    await move_watermark(LATER)
    assert await confirm_stamped_cards(session, now=LATER, quarantine=timedelta(hours=72)) == (
        0,
        0,
        0,
    )
    await session.commit()
    assert (await run_pass()).alerts_queued == 0
