"""The digest sender (NEU-1381): who gets a digest on which cadence, what the weekly slate
carries, and what every row's status says afterwards.

The decision pass is `test_notify_pass.py`'s subject, so the backlog here is seeded directly,
as `test_alert_sender.py` does: a `queued` digest row is the contract between the two passes.
Every run takes a fixed `today`, so the slate window is a fact of the fixture rather than of
the wall clock.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from upmovies.app.models import Notification, UserSettings, WatchlistItem
from upmovies.app.services.digest_sender import (
    DIGEST_BEAT_LABELS,
    SLATE_WINDOW_DAYS,
    digest_beat_label,
    digest_detail,
    send_digests,
)
from upmovies.config import get_settings
from upmovies.ingest.runs import create_run
from upmovies.mail import MailError, MailGateway, MessageId, NoopTransport
from upmovies.news.models import Event

TODAY = date(2026, 9, 18)
GRANTED = datetime(2027, 1, 1, tzinfo=UTC)
LAPSED = datetime(2026, 1, 1, tzinfo=UTC)
VERIFIED = datetime(2026, 1, 1, tzinfo=UTC)
NEWER_DAY = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
OLDER_DAY = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
BASE_URL = "https://app.example.test"
IMAGE_BASE = "https://image.tmdb.test/t/p"


def _on(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


@pytest.fixture
def settings():
    return get_settings().model_copy(
        update={
            "public_base_url": BASE_URL,
            "tmdb_image_base": IMAGE_BASE,
            "product_name": "Backlotter",
        }
    )


@pytest.fixture
def subscriber(make_user):
    """The ordinary recipient: verified and holding a live grant. No settings row, so the
    cadence is the default — weekly (D-33)."""

    async def _make(email: str = "sub@example.com", **kwargs):
        kwargs.setdefault("entitled_until", GRANTED)
        kwargs.setdefault("email_verified_at", VERIFIED)
        return await make_user(email=email, **kwargs)

    return _make


@pytest.fixture
def set_cadence(session):
    counter = {"n": 0}

    async def _set(user_id: UUID, cadence: str) -> None:
        counter["n"] += 1
        session.add(
            UserSettings(user_id=user_id, digest_cadence=cadence, ical_token=f"tok-{counter['n']}")
        )
        await session.commit()

    return _set


@pytest.fixture
def queue_digest(session):
    async def _queue(
        *, user_id: UUID, event_id: UUID, kind: str = "digest", channel: str = "email"
    ):
        row = Notification(
            user_id=user_id, event_id=event_id, kind=kind, channel=channel, status="queued"
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    return _queue


@pytest.fixture
def watchlist(session):
    async def _add(*, user_id: UUID, film_id: UUID) -> None:
        session.add(WatchlistItem(user_id=user_id, film_id=film_id, source="manual"))
        await session.commit()

    return _add


@pytest.fixture
def send(session_factory, settings):
    """Run the pass the way `pipeline_run.run_digest_stage` does — its own run row, its own
    sessions, one gateway over the whole pass."""

    async def _send(cadence: str = "weekly", *, transport=None, today: date = TODAY):
        transport = transport or NoopTransport()
        async with session_factory() as s:
            run_id = await create_run(s, kind="digest")
            await s.commit()
        async with MailGateway(settings, transport=transport) as mailer:
            result = await send_digests(
                session_factory=session_factory,
                run_id=run_id,
                cadence=cadence,  # type: ignore[arg-type]
                today=today,
                mailer=mailer,
                settings=settings,
            )
        return result, transport

    return _send


async def _rows(session) -> list[Notification]:
    return list(
        (
            await session.execute(
                select(Notification).order_by(Notification.created_at, Notification.id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


class BrokenTransport:
    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, envelope):
        self.attempts += 1
        raise MailError("the provider said no")

    async def aclose(self) -> None:
        pass


# --- the ticket's "done when": the weekly digest, slate and grouped events -----


async def test_the_weekly_digest_carries_the_slate_and_the_timeline_grouped_by_day_and_film(
    session, subscriber, make_film, add_event, add_release_date, queue_digest, watchlist, send
):
    """One mail: the slate first (the two upcoming US dates for the watchlisted film), then
    the timeline — newest day first, each film once per day with its events under it."""
    user = await subscriber()
    dune = await make_film(slug="dune", title="Dune: Part Three", poster_path="/dune.jpg")
    heat = await make_film(slug="heat-2", title="Heat 2")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_type=3, release_date=_on(TODAY + timedelta(days=7)))
    await add_release_date(film=dune, release_type=4, release_date=_on(TODAY + timedelta(days=21)))
    cast_1 = await add_event(
        film=heat, event_type="casting", created_at=NEWER_DAY, summary="Ada joined the cast."
    )
    cast_2 = await add_event(
        film=heat,
        event_type="production_start",
        created_at=NEWER_DAY + timedelta(hours=1),
        summary="Cameras are rolling.",
    )
    trailer = await add_event(
        film=dune, event_type="trailer", created_at=OLDER_DAY, summary="A trailer landed."
    )
    for event in (cast_1, cast_2, trailer):
        await queue_digest(user_id=user.id, event_id=event.id)

    result, mailbox = await send("weekly")

    assert (result.mails_sent, result.sent, result.slate_dates) == (1, 3, 2)
    (envelope,) = mailbox.sent
    assert envelope.to == "sub@example.com"
    assert envelope.subject == "Your slate: 2 upcoming dates and 3 updates"
    text = envelope.text
    # The slate: both dates, soonest first, each naming its release kind and the film.
    assert text.index("Friday, September 25, 2026") < text.index("Friday, October 9, 2026")
    assert text.index("Friday, September 25, 2026") < text.index("Wide release")
    assert text.index("Friday, October 9, 2026") < text.index("Digital release")
    # The timeline: the newer day leads, the film's two events sit under one heading.
    assert text.index("Thursday, September 17, 2026") < text.index("Tuesday, September 15, 2026")
    assert text.index("Heat 2") < text.index("Casting: Ada joined the cast.")
    assert text.index("Casting: Ada joined the cast.") < text.index(
        "Production started: Cameras are rolling."
    )
    assert text.index("Tuesday, September 15, 2026") < text.index("New trailer: A trailer landed.")
    assert text.count("Heat 2") == 1
    assert f"{BASE_URL}/film/{dune.tmdb_id}-dune-part-three" in text
    assert f"{BASE_URL}/settings" in text
    assert f"{IMAGE_BASE}/w154/dune.jpg" in envelope.html
    assert [row.status for row in await _rows(session)] == ["sent"] * 3
    assert all(row.sent_at is not None for row in await _rows(session))


async def test_the_daily_digest_carries_no_slate(
    session,
    subscriber,
    make_film,
    add_event,
    add_release_date,
    queue_digest,
    watchlist,
    set_cadence,
    send,
):
    user = await subscriber()
    await set_cadence(user.id, "daily")
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=7)))
    event = await add_event(film=dune, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)

    result, mailbox = await send("daily")

    assert (result.mails_sent, result.sent, result.slate_dates) == (1, 1, 0)
    (envelope,) = mailbox.sent
    assert envelope.subject == "Your daily digest: 1 update"
    assert "slate" not in envelope.text.lower()


# --- cadence -------------------------------------------------------------------


async def test_each_slot_mails_only_the_users_on_its_cadence(
    session, subscriber, make_film, add_event, queue_digest, set_cadence, send
):
    """Daily excludes weekly users and vice versa; a user with no settings row is weekly, the
    default; `off` matches neither and their rows are left alone."""
    ada = await subscriber("ada@example.com")
    bob = await subscriber("bob@example.com")
    cy = await subscriber("cy@example.com")
    dee = await subscriber("dee@example.com")
    await set_cadence(ada.id, "daily")
    await set_cadence(cy.id, "weekly")
    await set_cadence(dee.id, "off")
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    for user in (ada, bob, cy, dee):
        await queue_digest(user_id=user.id, event_id=event.id)

    daily, daily_box = await send("daily")
    weekly, weekly_box = await send("weekly")

    assert (daily.users_considered, daily.mails_sent) == (1, 1)
    assert [e.to for e in daily_box.sent] == ["ada@example.com"]
    assert (weekly.users_considered, weekly.mails_sent) == (2, 2)
    assert sorted(e.to for e in weekly_box.sent) == ["bob@example.com", "cy@example.com"]
    statuses = {row.user_id: row.status for row in await _rows(session)}
    assert statuses == {ada.id: "sent", bob.id: "sent", cy.id: "sent", dee.id: "queued"}


async def test_a_user_with_nothing_queued_and_an_empty_slate_gets_no_mail(
    session, subscriber, make_film, add_release_date, watchlist, send
):
    user = await subscriber()
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=60)))

    result, mailbox = await send("weekly")

    assert (result.users_considered, result.mails_sent) == (1, 0)
    assert mailbox.sent == []


async def test_a_slate_alone_is_a_weekly_mail(
    session, subscriber, make_film, add_release_date, watchlist, send
):
    """The slate needs no notification row behind it (D-33): a quiet week with a date coming
    up is still a mail."""
    user = await subscriber()
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))

    result, mailbox = await send("weekly")

    assert (result.mails_sent, result.sent, result.slate_dates) == (1, 0, 1)
    (envelope,) = mailbox.sent
    assert envelope.subject == "Your slate: 1 upcoming date"


async def test_a_second_run_sends_nothing_because_the_rows_are_sent(
    session, subscriber, make_film, add_event, queue_digest, send
):
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)
    await send("weekly")

    result, mailbox = await send("weekly")

    assert (result.users_considered, result.mails_sent) == (0, 0)
    assert mailbox.sent == []


async def test_alert_rows_and_push_rows_are_not_this_pass_s_work(
    session, subscriber, make_film, add_event, queue_digest, send
):
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id, kind="alert")
    await queue_digest(user_id=user.id, event_id=event.id, channel="push")

    result, mailbox = await send("weekly")

    assert (result.users_considered, result.mails_sent) == (0, 0)
    assert mailbox.sent == []
    assert {row.status for row in await _rows(session)} == {"queued"}


# --- the slate window ----------------------------------------------------------


async def test_the_slate_is_the_governing_us_date_per_release_type_inside_the_window(
    session, subscriber, make_film, add_release_date, watchlist, send
):
    """Thirty dates, today first: today and today + 29 are in; yesterday and today + 30 are
    out. A non-US date, a premiere and a film not on the watchlist never appear; of two US
    wide rows the earliest governs (NEU-1206)."""
    user = await subscriber()
    edge = await make_film(slug="edge", title="Edge")
    dune = await make_film(slug="dune", title="Dune")
    other = await make_film(slug="other", title="Other")
    for film in (edge, dune):
        await watchlist(user_id=user.id, film_id=film.id)
    await add_release_date(film=edge, release_type=3, release_date=_on(TODAY))
    await add_release_date(
        film=edge, release_type=4, release_date=_on(TODAY + timedelta(days=SLATE_WINDOW_DAYS - 1))
    )
    await add_release_date(
        film=edge, release_type=5, release_date=_on(TODAY + timedelta(days=SLATE_WINDOW_DAYS))
    )
    await add_release_date(film=dune, release_type=2, release_date=_on(TODAY - timedelta(days=1)))
    await add_release_date(film=dune, release_type=3, release_date=_on(TODAY + timedelta(days=20)))
    await add_release_date(film=dune, release_type=3, release_date=_on(TODAY + timedelta(days=10)))
    await add_release_date(
        film=dune, release_type=3, iso_3166_1="GB", release_date=_on(TODAY + timedelta(days=2))
    )
    await add_release_date(film=dune, release_type=1, release_date=_on(TODAY + timedelta(days=4)))
    await add_release_date(film=other, release_type=3, release_date=_on(TODAY + timedelta(days=5)))

    result, mailbox = await send("weekly")

    assert result.slate_dates == 3
    text = mailbox.sent[0].text
    assert "Friday, September 18, 2026" in text  # today, wide
    assert "Monday, September 28, 2026" in text  # dune's earliest US wide row
    assert "Saturday, October 17, 2026" in text  # today + 29, digital
    assert "Physical release" not in text  # today + 30
    assert "Thursday, September 17, 2026" not in text  # yesterday
    assert "Sunday, September 20, 2026" not in text  # GB
    assert "Tuesday, September 22, 2026" not in text  # premiere
    assert "Thursday, October 8, 2026" not in text  # dune's later US wide row
    assert "Other" not in text


async def test_a_watchlisted_film_with_no_slug_is_not_on_the_slate(
    session, subscriber, make_film, add_release_date, watchlist, send
):
    user = await subscriber()
    film = await make_film(slug=None, title="Unpaged")  # type: ignore[arg-type]
    await watchlist(user_id=user.id, film_id=film.id)
    await add_release_date(film=film, release_date=_on(TODAY + timedelta(days=3)))

    result, mailbox = await send("weekly")

    assert (result.mails_sent, result.slate_dates) == (0, 0)


# --- the access gate, re-read at send time and covering the slate (D-37, D-39) -


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("entitled_until", LAPSED, "entitlement lapsed"),
        ("email_verified_at", None, "unverified"),
    ],
)
async def test_a_user_the_gate_refuses_gets_no_digest_and_no_slate(
    session,
    subscriber,
    make_film,
    add_event,
    add_release_date,
    queue_digest,
    watchlist,
    send,
    field,
    value,
    why,
):
    """The ticket's test: rows queued while the grant was live, the grant lapsed since. No
    mail, the rows `suppressed` — and no slate either, which is the case only this pass can
    get wrong, since the slate is built from a watchlist D-40 keeps intact."""
    user = await subscriber(**{field: value})
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))
    event = await add_event(film=dune, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)

    result, mailbox = await send("weekly")

    assert (result.mails_sent, result.sent, result.suppressed, result.users_gated) == (
        0,
        0,
        1,
        1,
    ), why
    assert result.slate_dates == 0
    assert mailbox.sent == []
    (row,) = await _rows(session)
    assert (row.status, row.sent_at, row.error) == ("suppressed", None, None)


async def test_a_refused_user_with_only_a_slate_is_counted_as_gated(
    session, subscriber, make_film, add_release_date, watchlist, send
):
    """No row to suppress, so `users_gated` is the only trace that the user was considered
    and refused rather than never looked at."""
    user = await subscriber(entitled_until=LAPSED)
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))

    result, mailbox = await send("weekly")

    assert (result.users_considered, result.mails_sent, result.users_gated) == (1, 0, 1)
    assert mailbox.sent == []


async def test_one_refused_user_does_not_cost_the_next_their_digest(
    session, subscriber, make_film, add_event, queue_digest, send
):
    ada = await subscriber("ada@example.com", entitled_until=LAPSED)
    bob = await subscriber("bob@example.com")
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=ada.id, event_id=event.id)
    await queue_digest(user_id=bob.id, event_id=event.id)

    result, mailbox = await send("weekly")

    assert (result.mails_sent, result.sent, result.suppressed) == (1, 1, 1)
    assert [e.to for e in mailbox.sent] == ["bob@example.com"]


# --- rows that can never be sent -----------------------------------------------


async def test_a_superseded_event_and_a_summaryless_one_fail_their_rows_with_the_reason(
    session, subscriber, make_film, add_event, queue_digest, send
):
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    good = await add_event(film=film, event_type="casting", created_at=NEWER_DAY, summary="Fine.")
    stale = await add_event(film=film, event_type="trailer", created_at=NEWER_DAY, summary="Old.")
    bare = await add_event(film=film, event_type="announced", created_at=NEWER_DAY, summary=None)
    for event in (good, stale, bare):
        await queue_digest(user_id=user.id, event_id=event.id)
    await session.execute(update(Event).where(Event.id == stale.id).values(status="superseded"))
    await session.commit()

    result, mailbox = await send("weekly")

    assert (result.mails_sent, result.sent, result.failed) == (1, 1, 2)
    (envelope,) = mailbox.sent
    assert "Fine." in envelope.text
    assert "Old." not in envelope.text
    by_event = {row.event_id: row for row in await _rows(session)}
    assert by_event[good.id].status == "sent"
    assert (by_event[stale.id].status, by_event[stale.id].error) == (
        "failed",
        "the event is no longer published",
    )
    assert (by_event[bare.id].status, by_event[bare.id].error) == (
        "failed",
        "the event has no summary",
    )


async def test_only_unsendable_rows_and_no_slate_is_no_mail(
    session, subscriber, make_film, add_event, queue_digest, send
):
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    bare = await add_event(film=film, event_type="announced", created_at=NEWER_DAY, summary=None)
    await queue_digest(user_id=user.id, event_id=bare.id)

    result, mailbox = await send("weekly")

    assert (result.mails_sent, result.failed) == (0, 1)
    assert mailbox.sent == []


# --- the provider ----------------------------------------------------------------


async def test_a_provider_failure_marks_every_row_the_mail_carried_failed(
    session, subscriber, make_film, add_event, queue_digest, send
):
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    for event_type in ("casting", "trailer"):
        event = await add_event(film=film, event_type=event_type, created_at=NEWER_DAY)
        await queue_digest(user_id=user.id, event_id=event.id)

    result, transport = await send("weekly", transport=BrokenTransport())

    assert (result.mails_sent, result.sent, result.failed) == (0, 0, 2)
    assert transport.attempts == 1
    rows = await _rows(session)
    assert [row.status for row in rows] == ["failed", "failed"]
    assert all(row.error == "MailError: the provider said no" for row in rows)


async def test_consecutive_provider_failures_abort_the_pass(
    session, subscriber, make_film, add_event, queue_digest, session_factory, settings
):
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    for n in range(4):
        user = await subscriber(f"user{n}@example.com")
        await queue_digest(user_id=user.id, event_id=event.id)
    transport = BrokenTransport()

    async with session_factory() as s:
        run_id = await create_run(s, kind="digest")
        await s.commit()
    async with MailGateway(settings, transport=transport) as mailer:
        result = await send_digests(
            session_factory=session_factory,
            run_id=run_id,
            cadence="weekly",
            today=TODAY,
            mailer=mailer,
            settings=settings,
            failure_threshold=2,
        )

    assert result.aborted is True
    assert result.abort_error is not None
    assert "provider failures" in result.abort_error
    assert transport.attempts == 2
    assert sorted(row.status for row in await _rows(session)) == [
        "failed",
        "failed",
        "queued",
        "queued",
    ]


async def test_a_run_of_failures_that_recovers_does_not_abort(
    session, subscriber, make_film, add_event, queue_digest, send
):
    class FlakyTransport:
        def __init__(self) -> None:
            self.attempts = 0
            self.sent = []

        async def send(self, envelope):
            self.attempts += 1
            if self.attempts == 1:
                raise MailError("the provider said no")
            self.sent.append(envelope)
            return MessageId("flaky-ok")

        async def aclose(self) -> None:
            pass

    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    for n in range(3):
        user = await subscriber(f"user{n}@example.com")
        await queue_digest(user_id=user.id, event_id=event.id)

    result, _ = await send("weekly", transport=FlakyTransport())

    assert result.aborted is False
    assert (result.sent, result.failed) == (2, 1)


# --- the detail line and the small rules ---------------------------------------


async def test_the_detail_line_reports_the_cadence_mails_rows_and_the_slate(
    session, subscriber, make_film, add_event, add_release_date, queue_digest, watchlist, send
):
    user = await subscriber()
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))
    event = await add_event(film=dune, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)

    result, _ = await send("weekly")

    assert digest_detail(result) == (
        "digest weekly: 1 mails to 1 users, 1 sent, 0 failed, 0 suppressed, 0 gated, "
        "1 slate dates, 0 lost"
    )


async def test_an_unknown_cadence_is_refused_before_anything_is_read(session_factory, settings):
    with pytest.raises(ValueError, match="cadence"):
        await send_digests(
            session_factory=session_factory,
            run_id=UUID(int=0),
            cadence="off",  # type: ignore[arg-type]
            today=TODAY,
            mailer=MailGateway(settings, transport=NoopTransport()),
            settings=settings,
        )


def test_every_visible_event_type_has_a_digest_label_and_an_unknown_one_still_reads():
    """The digest is the timeline, so every type `ck_event_type` admits — bar the hidden
    `other`, which is never queued — must read as something better than 'Update'."""
    visible = (
        "announced",
        "casting",
        "credit_removed",
        "crew_attached",
        "now_available",
        "production_start",
        "production_wrap",
        "release_date",
        "trailer",
        "first_look",
    )
    assert set(DIGEST_BEAT_LABELS) == set(visible)
    for event_type in visible:
        assert digest_beat_label(event_type) != "Update"
    assert digest_beat_label("release_date") == "Release date"
    assert digest_beat_label("bogus") == "Update"
