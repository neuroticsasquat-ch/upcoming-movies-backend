"""The M7 decision pass (NEU-1379): who gets an alert, who gets a digest line, and who gets a
`suppressed` row instead of mail.

Every test here seeds a watermark by hand — a `succeeded` notify run at a fixed instant — and
dates its events either side of it, so the window is explicit rather than a function of when
the suite happened to run. `run_notify_pass` reads that watermark exactly as production does.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from upmovies.app.models import (
    Follow,
    Notification,
    PushSubscription,
    UserSettings,
    WatchlistDismissal,
)
from upmovies.app.services.notify_service import (
    NotifyResult,
    notify_detail,
    run_notify_pass,
)
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run, finalize_run
from upmovies.news.models import Story, StoryPerson

WATERMARK = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)
"""When the last successful notify run began. Events created after it are this pass's work."""
NEW = datetime(2026, 9, 18, 3, 0, tzinfo=UTC)
OLD = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
BETWEEN = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
"""Where the watermark lands after a first pass — between the two windows below."""
LATER = datetime(2026, 9, 19, 3, 0, tzinfo=UTC)
TODAY = date(2026, 9, 18)
EXCLUDED = frozenset({"Released", "Canceled"})
"""`TMDB_EXCLUDED_STATUSES`' default. Only the **digest** branch reads it now (D-11's timeline
builder); the alert window's status term is a constant, `Canceled` alone (D-46)."""
MAX_AGE_DAYS = 365
"""The alert window's width — `PROVIDER_POLL_MAX_AGE_DAYS`' default, pinned here the way the
statuses are, so a film's coverage does not depend on the environment the suite runs in."""
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

    async def _run(*, today: date = TODAY) -> NotifyResult:
        async with session_factory() as s:
            run_id = await create_run(s, kind="notify")
            await s.commit()
        result = await run_notify_pass(
            session_factory=session_factory,
            run_id=run_id,
            today=today,
            excluded_statuses=EXCLUDED,
            max_age_days=MAX_AGE_DAYS,
        )
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


async def _follow_person(session, *, user_id: UUID, person_id: int, coverage: str = "lead") -> None:
    session.add(
        Follow(
            user_id=user_id,
            entity_type="person",
            entity_id=str(person_id),
            source="manual",
            coverage=coverage,
        )
    )
    await session.commit()


async def _set_alert_stores(session, *, user_id: UUID, alert_stores: list[str]) -> None:
    session.add(
        UserSettings(user_id=user_id, alert_stores=alert_stores, ical_token=f"tok-{user_id}")
    )
    await session.commit()


async def _mute(session, *, user_id: UUID, film_id: UUID) -> None:
    session.add(WatchlistDismissal(user_id=user_id, film_id=film_id))
    await session.commit()


async def test_a_title_follow_on_a_whitelist_beat_queues_an_alert_and_a_digest(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """Both branches from one row, which is M8's whole shape: the film is on this user's
    watchlist *and* on their timeline, and an alert and a digest line are different deliveries
    of the same news (`app.models.Notification`)."""
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


async def test_a_person_follow_outside_its_coverage_earns_a_digest_but_no_alert(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """Where the two branches come apart now that one row feeds both (D-43). A writing credit
    is seed grade, so the film is on the timeline and in the digest; it is not `lead`, so the
    default coverage does not put it on the watchlist and nothing alerts — on a whitelist beat,
    so the only thing keeping it out of the alert branch is the tier."""
    await seed_watermark()
    user = await subscriber()
    # Dated ahead of `TODAY`, so the timeline's in-play term (D-11) is satisfied and the only
    # thing the two branches can disagree about is the coverage tier.
    film = await make_film(slug="dune", title="Dune", release_date=date(2099, 1, 1))
    await attach_credits(film, crew=[{"id": 488, "name": "A Writer", "job": "Screenplay"}])
    await _follow_person(session, user_id=user.id, person_id=488)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 1)
    (row,) = await _rows(session)
    assert (row.kind, row.status) == ("digest", "queued")


async def test_widening_the_coverage_puts_the_same_film_in_the_alert_branch(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """The same graph at `coverage = 'major'`: the credit now covers the film for alerts too, and
    nothing else about the pass changes."""
    await seed_watermark()
    user = await subscriber()
    # Dated ahead of `TODAY`, so the timeline's in-play term (D-11) is satisfied and the only
    # thing the two branches can disagree about is the coverage tier.
    film = await make_film(slug="dune", title="Dune", release_date=date(2099, 1, 1))
    await attach_credits(film, crew=[{"id": 488, "name": "A Writer", "job": "Screenplay"}])
    await _follow_person(session, user_id=user.id, person_id=488, coverage="major")
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (1, 1)


async def test_a_muted_film_earns_neither_kind(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """D-45 as amended: a mute silences the film everywhere, so it leaves the digest branch
    beside the alert one. The follow is untouched — this is reversible (D-40)."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await _mute(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 0)
    assert await _rows(session) == []
    assert (await session.execute(select(Follow))).scalars().all() != []


async def test_a_beat_outside_the_whitelist_queues_no_alert(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """D-32 is a closed list: a casting announcement is digest material, never an alert — the
    beat is what decides, not how the film got onto the list."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="casting", created_at=NEW)

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (0, 1)
    assert [row.kind for row in await _rows(session)] == ["digest"]


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


async def test_a_director_follow_alerts_on_a_released_films_now_available_beat(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    seed_watermark,
    run_pass,
):
    """The alert NEU-1417 is about (D-46). The user follows nobody but the director; the film
    opened two months ago and TMDB has marked it `Released`, which under NEU-1414's window took
    it off their watchlist on release day — the exact morning the `now_available` beat it was
    waiting for arrives. The window's status term now ends at `Canceled`, so the alert is owed
    and sent.

    The digest line is absent on purpose: timeline coverage is still D-11's in-play cut, so a
    released film reaches the alert branch and not the digest branch through the same follow.
    """
    await seed_watermark()
    user = await subscriber()
    film = await make_film(
        slug="dune", title="Dune", release_date=TODAY - timedelta(days=60), status="Released"
    )
    await attach_credits(film, crew=[{"id": 525, "name": "A Director", "job": "Director"}])
    await _follow_person(session, user_id=user.id, person_id=525)
    await add_event(
        film=film,
        event_type="now_available",
        provenance="catalog",
        region="US",
        subject_key=["US:flatrate"],
        created_at=NEW,
    )

    result = await run_pass()

    assert (result.alerts_queued, result.digests_queued) == (1, 0)
    (alert,) = await _rows(session)
    assert (alert.user_id, alert.kind) == (user.id, "alert")


async def test_a_rumored_event_is_never_queued_in_any_kind(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """ "Nothing `unconfirmed` is ever queued as an alert" (D-32) — and it is not digest
    material either, so the cut lives in the window rather than in one branch."""
    await seed_watermark()
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="release_date", confidence="rumored", created_at=NEW)

    result = await run_pass()

    assert result.events_considered == 0
    assert await _rows(session) == []


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


async def test_the_digest_covers_events_that_merely_name_a_followed_person(
    session,
    session_factory,
    subscriber,
    make_film,
    add_event,
    make_person,
    seed_watermark,
    run_pass,
):
    """D-11's second half, which `/me/timeline` OR-s in beside the film filter. A digest that
    covered less than the timeline it summarises would be exactly the drift
    `app.follow_queries` exists to prevent."""
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
            prompt_version="1",
        )
    )
    await session.commit()

    result = await run_pass()

    assert result.digests_queued == 1
    (row,) = await _rows(session)
    assert (row.event_id, row.kind) == (event.id, "digest")


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
    has a push half."""
    await seed_watermark()
    user = await subscriber()
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="casting", created_at=NEW)

    result = await run_pass()

    assert (result.digests_queued, result.push_alerts_queued) == (1, 0)
    assert [(row.kind, row.channel) for row in await _rows(session)] == [("digest", "email")]


async def test_a_beat_outside_the_whitelist_queues_no_push_either(
    session, session_factory, subscriber, make_film, add_event, seed_watermark, run_pass
):
    """The push branch inherits D-32 rather than restating it."""
    await seed_watermark()
    user = await subscriber()
    await _register_push(session, user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    await _follow_title(session, user_id=user.id, film_id=film.id)
    await add_event(film=film, event_type="casting", created_at=NEW)

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
