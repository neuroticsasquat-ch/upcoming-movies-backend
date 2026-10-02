"""The digest sender (NEU-1381, NEU-1460, NEU-1462, NEU-1528): who gets a digest on which
cadence, what the slate carries and on which day the daily carries it, how the cards a user's
follows reached are laid out as the timeline lays them out, and what every row's status says
afterwards.

The decision pass is `test_notify_pass.py`'s subject, so the backlog here is seeded directly: a
`queued` digest row is the contract between the two passes.
Every run takes a fixed `today`, so the slate window is a fact of the fixture rather than of
the wall clock.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update

from upmovies.app.models import Follow, Notification, UserSettings
from upmovies.app.services.digest_sender import (
    DIGEST_BEAT_LABELS,
    SLATE_WINDOW_DAYS,
    digest_beat_label,
    digest_detail,
    render_digest,
    send_digests,
)
from upmovies.catalog.models import FilmReleaseDateChange
from upmovies.config import get_settings
from upmovies.ingest.runs import create_run
from upmovies.mail import MailError, MailGateway, MessageId, NoopTransport
from upmovies.news.models import Event
from upmovies.news.subject_key import company_subject_token, normalize_name

TODAY = date(2026, 9, 18)
"""A Friday — off the default `SLATE_WEEKDAY`, so a daily send on it carries no slate."""
SLATE_DAY = date(2026, 9, 17)
"""A Thursday, the default `SLATE_WEEKDAY` (DC-2)."""
GRANTED = datetime(2027, 1, 1, tzinfo=UTC)
LAPSED = datetime(2026, 1, 1, tzinfo=UTC)
VERIFIED = datetime(2026, 1, 1, tzinfo=UTC)
NEWER_DAY = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
OLDER_DAY = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
BASE_URL = "https://app.example.test"
API_BASE = "https://api.example.test"
IMAGE_BASE = "https://image.tmdb.test/t/p"


def _on(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


@pytest.fixture
def settings():
    return get_settings().model_copy(
        update={
            "public_base_url": BASE_URL,
            "api_base_url": API_BASE,
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
            UserSettings(
                user_id=user_id,
                digest_cadence=cadence,
                ical_token=f"tok-{counter['n']}",
                unsubscribe_token=f"unsub-{counter['n']}",
            )
        )
        await session.commit()

    return _set


@pytest.fixture
def queue_digest(session):
    """A `queued` digest row — and, unless `follow_title=False`, the title follow that reaches
    it: a card no follow reaches is not a line in the mail (FB-25), and most tests here are
    about a followed film's card, not about how it was reached."""

    async def _queue(*, user_id: UUID, event_id: UUID, follow_title: bool = True):
        if follow_title:
            film_id = await session.scalar(select(Event.film_id).where(Event.id == event_id))
            followed = await session.scalar(
                select(Follow.user_id).where(
                    Follow.user_id == user_id,
                    Follow.entity_type == "title",
                    Follow.entity_id == str(film_id),
                )
            )
            if followed is None:
                session.add(
                    Follow(
                        user_id=user_id,
                        entity_type="title",
                        entity_id=str(film_id),
                        source="manual",
                    )
                )
        row = Notification(
            user_id=user_id, event_id=event_id, kind="digest", channel="email", status="queued"
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    return _queue


@pytest.fixture
def watchlist(session):
    """Follow a film by title — the whole of what puts it on the slate (EF-14)."""

    async def _add(*, user_id: UUID, film_id: UUID) -> None:
        session.add(
            Follow(
                user_id=user_id,
                entity_type="title",
                entity_id=str(film_id),
                source="manual",
            )
        )
        await session.commit()

    return _add


@pytest.fixture
def send(session_factory, settings):
    """Run the pass the way `pipeline_run.run_digest_stage` does — its own run row, its own
    sessions, one gateway over the whole pass."""

    async def _send(
        cadence: str = "weekly", *, transport=None, today: date = TODAY, overrides=None
    ):
        pass_settings = settings.model_copy(update=overrides or {})
        transport = transport or NoopTransport()
        async with session_factory() as s:
            run_id = await create_run(s, kind="digest")
            await s.commit()
        async with MailGateway(pass_settings, transport=transport) as mailer:
            result = await send_digests(
                session_factory=session_factory,
                run_id=run_id,
                cadence=cadence,  # type: ignore[arg-type]
                today=today,
                mailer=mailer,
                settings=pass_settings,
            )
        return result, transport

    return _send


@pytest.fixture
def follow(session):
    """An entity or title follow, in the follow graph's words (`person`, `company`, …)."""

    async def _add(*, user_id: UUID, entity_type: str, entity_id: str) -> None:
        session.add(
            Follow(user_id=user_id, entity_type=entity_type, entity_id=entity_id, source="manual")
        )
        await session.commit()

    return _add


def _timeline(part: str) -> str:
    """Either part from the timeline heading on — the slate and the preheader name films too."""
    heading = "NEW ON YOUR TIMELINE" if "NEW ON YOUR TIMELINE" in part else "New on your timeline"
    return part[part.index(heading) :]


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


# --- the weekly digest: the slate, then the timeline -------------------------


async def test_the_weekly_digest_carries_the_slate_and_the_timeline_under_follow_blocks(
    session, subscriber, make_film, add_event, add_release_date, queue_digest, watchlist, send
):
    """One mail: the slate first (the two upcoming US dates for the watchlisted film), then
    the timeline — the trailer outranks the production start, so Dune leads and names the
    subject — laid out under Films by update type, with no day headings and every line
    dated (the weekly reads by entry, FB-19)."""
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
        occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
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
    assert envelope.subject == "Dune: Part Three — new trailer, + 1 more film · your slate"
    text = envelope.text
    # The slate: both dates, soonest first, each naming its release kind and the film.
    assert text.index("Friday, September 25, 2026") < text.index("Friday, October 9, 2026")
    assert text.index("Friday, September 25, 2026") < text.index("Wide release")
    assert text.index("Friday, October 9, 2026") < text.index("Digital release")
    # The timeline: Not yet reported's headings in the feed's order (NR-3), each line dated
    # and with no beat label under a heading that names its type.
    timeline = _timeline(text)
    assert "FILMS\n\nNot yet reported (unconfirmed)\n" in timeline
    assert timeline.index("-- Trailer --") < timeline.index("-- Production status --")
    assert timeline.index("-- Production status --") < timeline.index("-- Cast --")
    assert "  15 Sep · A trailer landed.\n" in timeline
    assert "  17 Sep · Cameras are rolling.\n" in timeline
    assert "  17 Sep · Ada joined the cast.\n" in timeline
    assert "September 17, 2026" not in timeline
    assert "September 15, 2026" not in timeline
    assert f"{BASE_URL}/film/{dune.tmdb_id}-dune-part-three" in text
    assert f"{BASE_URL}/settings" in text
    # Dune is on the slate as a 62px row and in the timeline's poster strip at 52px.
    assert f'<img src="{IMAGE_BASE}/w154/dune.jpg" width="62"' in envelope.html
    assert f'<img src="{IMAGE_BASE}/w154/dune.jpg" width="52"' in envelope.html
    assert [row.status for row in await _rows(session)] == ["sent"] * 3
    assert all(row.sent_at is not None for row in await _rows(session))


async def test_the_daily_digest_carries_no_slate_off_the_slate_day(
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
    assert envelope.subject == "Dune — casting"
    assert "slate" not in envelope.text.lower()


# --- the slate day (NEU-1462, DC-2) --------------------------------------------


async def test_the_daily_digest_carries_the_slate_on_the_slate_day(
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
    """The same reader and backlog as the test above, one day earlier: on the slate day the
    daily puts the slate in front of the cards."""
    user = await subscriber()
    await set_cadence(user.id, "daily")
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(SLATE_DAY + timedelta(days=7)))
    event = await add_event(film=dune, event_type="casting", created_at=_on(SLATE_DAY, 6))
    await queue_digest(user_id=user.id, event_id=event.id)

    result, mailbox = await send("daily", today=SLATE_DAY)

    assert (result.mails_sent, result.sent, result.slate_dates) == (1, 1, 1)
    (envelope,) = mailbox.sent
    assert envelope.subject == "Dune — casting · your slate"
    text = envelope.text
    assert text.index("YOUR SLATE") < text.index("NEW ON YOUR TIMELINE")
    assert "Thursday, September 24, 2026" in text


async def test_a_daily_reader_with_an_empty_queue_gets_the_slate_on_the_slate_day_only(
    session, subscriber, make_film, add_release_date, watchlist, set_cadence, send
):
    """DC-2's consequence: nothing queued and a non-empty slate is a mail on the slate day —
    the weekly's rule — and on any other day the reader is not even considered."""
    user = await subscriber()
    await set_cadence(user.id, "daily")
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(SLATE_DAY + timedelta(days=3)))

    off_day, off_box = await send("daily", today=TODAY)
    slate_day, slate_box = await send("daily", today=SLATE_DAY)

    assert (off_day.users_considered, off_day.mails_sent) == (0, 0)
    assert off_box.sent == []
    assert (slate_day.users_considered, slate_day.mails_sent, slate_day.slate_dates) == (1, 1, 1)
    (envelope,) = slate_box.sent
    assert envelope.subject == "Your slate: 1 upcoming date"


async def test_slate_weekday_moves_the_daily_slate_day(
    session, subscriber, make_film, add_release_date, watchlist, set_cadence, send
):
    user = await subscriber()
    await set_cadence(user.id, "daily")
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))

    thursday, _ = await send("daily", today=SLATE_DAY, overrides={"slate_weekday": "friday"})
    friday, friday_box = await send("daily", today=TODAY, overrides={"slate_weekday": "friday"})

    assert thursday.mails_sent == 0
    assert (friday.mails_sent, friday.slate_dates) == (1, 1)
    assert friday_box.sent[0].subject == "Your slate: 1 upcoming date"


async def test_the_weekly_carries_the_slate_whatever_day_it_runs(
    session, subscriber, make_film, add_release_date, watchlist, send
):
    """`SLATE_WEEKDAY` documents the weekly slot's day and does not gate it (DC-2): a weekly
    slot scheduled on the wrong day still sends the slate, rather than silently nothing."""
    user = await subscriber()
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))

    result, mailbox = await send("weekly", today=TODAY, overrides={"slate_weekday": "monday"})

    assert (result.mails_sent, result.slate_dates) == (1, 1)
    assert mailbox.sent[0].subject == "Your slate: 1 upcoming date"


async def test_a_daily_preview_on_the_slate_day_shows_the_slate(
    session, subscriber, make_film, add_release_date, watchlist, set_cadence, settings
):
    """`render_digest` follows the same day rule, so M3's preview shows what the slot sends."""
    user = await subscriber()
    await set_cadence(user.id, "daily")
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(SLATE_DAY + timedelta(days=3)))

    on_the_day = await render_digest(session, user.id, "daily", SLATE_DAY, settings)
    off_the_day = await render_digest(session, user.id, "daily", TODAY, settings)

    assert on_the_day is not None
    assert on_the_day.subject == "Your slate: 1 upcoming date"
    assert off_the_day is None


# --- slate markers (NEU-1462, DC-9) --------------------------------------------


@pytest.fixture
def record_date_change(session, add_event):
    """A release-date observation the way the sweep leaves it: the `film_release_date_change`
    row(s) and the catalog card whose `occurred_at` is their `changed_at` (`release_events`)."""

    async def _record(
        *,
        film,
        created_at: datetime,
        changes: list[tuple[int, str]],
        persisted: bool = True,
        summary: str = "A neutral summary.",
    ) -> Event:
        changed_at = created_at - timedelta(hours=1)
        if persisted:
            for release_type, change in changes:
                session.add(
                    FilmReleaseDateChange(
                        film_id=film.id,
                        iso_3166_1="US",
                        release_type=release_type,
                        previous_date=None if change == "set" else date(2026, 9, 1),
                        new_date=date(2026, 10, 1),
                        change=change,
                        changed_at=changed_at,
                    )
                )
        buckets = {2: "limited", 3: "wide", 4: "digital", 5: "physical"}
        return await add_event(
            film=film,
            event_type="release_date",
            provenance="catalog",
            occurred_at=changed_at,
            created_at=created_at,
            summary=summary,
            subject_key=[f"US:{buckets[t]}" for t, _ in changes],
            region="US",
        )

    return _record


async def test_a_slate_row_is_marked_new_or_moved_by_the_change_that_carded_it(
    session,
    subscriber,
    make_film,
    add_event,
    add_release_date,
    watchlist,
    record_date_change,
    send,
):
    """Set → `[new]`, moved → `[moved]`, untouched → nothing; each market of a film is marked
    by its own change, and a change older than the previous slate day marks nothing."""
    user = await subscriber()
    films = {}
    for n, title in enumerate(("Arrival", "Blade", "Casino", "Dune", "Edge")):
        films[title] = await make_film(slug=title.lower(), title=title)
        await watchlist(user_id=user.id, film_id=films[title].id)
        await add_release_date(film=films[title], release_date=_on(TODAY + timedelta(days=3 + n)))
    await add_release_date(
        film=films["Edge"], release_type=4, release_date=_on(TODAY + timedelta(days=12))
    )
    await record_date_change(
        film=films["Arrival"], created_at=_on(TODAY - timedelta(days=2)), changes=[(3, "set")]
    )
    await record_date_change(film=films["Blade"], created_at=_on(TODAY, 6), changes=[(3, "moved")])
    # Casino: untouched. Dune: moved on the previous slate day — last week's news.
    await record_date_change(
        film=films["Dune"],
        created_at=_on(TODAY - timedelta(days=7), 23),
        changes=[(3, "moved")],
    )
    # Edge: one card, and only the digital date moved.
    await record_date_change(
        film=films["Edge"], created_at=_on(TODAY - timedelta(days=6), 0), changes=[(4, "moved")]
    )

    result, mailbox = await send("weekly")

    assert result.slate_dates == 6
    (envelope,) = mailbox.sent
    lines = envelope.text.splitlines()
    assert "  Arrival — Wide release [new]" in lines
    assert "  Blade — Wide release [moved]" in lines
    assert "  Casino — Wide release" in lines
    assert "  Dune — Wide release" in lines
    assert "  Edge — Wide release" in lines
    assert "  Edge — Digital release [moved]" in lines
    assert envelope.html.count(">New</span>") == 1
    assert envelope.html.count(">Moved</span>") == 2


async def test_a_date_set_and_then_moved_since_the_last_slate_is_new(
    session, subscriber, make_film, add_release_date, watchlist, record_date_change, send
):
    user = await subscriber()
    film = await make_film(slug="arrival", title="Arrival")
    await watchlist(user_id=user.id, film_id=film.id)
    await add_release_date(film=film, release_date=_on(TODAY + timedelta(days=3)))
    await record_date_change(
        film=film, created_at=_on(TODAY - timedelta(days=3)), changes=[(3, "set")]
    )
    await record_date_change(film=film, created_at=_on(TODAY), changes=[(3, "moved")])

    _result, mailbox = await send("weekly")

    assert "  Arrival — Wide release [new]" in mailbox.sent[0].text.splitlines()


async def test_an_unpersisted_change_falls_back_to_the_summarys_verb(
    session, subscriber, make_film, add_release_date, watchlist, record_date_change, send
):
    user = await subscriber()
    arrival = await make_film(slug="arrival", title="Arrival")
    blade = await make_film(slug="blade", title="Blade")
    for n, film in enumerate((arrival, blade)):
        await watchlist(user_id=user.id, film_id=film.id)
        await add_release_date(film=film, release_date=_on(TODAY + timedelta(days=3 + n)))
    await record_date_change(
        film=arrival,
        created_at=_on(TODAY),
        changes=[(3, "set")],
        persisted=False,
        summary="US wide release date set to 21 September 2026.",
    )
    await record_date_change(
        film=blade,
        created_at=_on(TODAY),
        changes=[(3, "moved")],
        persisted=False,
        summary="US wide release date slipped from 1 September 2026 to 22 September 2026.",
    )

    _result, mailbox = await send("weekly")

    lines = mailbox.sent[0].text.splitlines()
    assert "  Arrival — Wide release [new]" in lines
    assert "  Blade — Wide release [moved]" in lines


async def test_only_a_published_us_release_date_card_marks_a_slate_row(
    session, subscriber, make_film, add_event, add_release_date, watchlist, send
):
    """A story-borne release-date card has no subject, a card about another market covers no
    slate row, and a trailer is not a date change: none of them marks anything."""
    user = await subscriber()
    film = await make_film(slug="arrival", title="Arrival")
    await watchlist(user_id=user.id, film_id=film.id)
    await add_release_date(film=film, release_date=_on(TODAY + timedelta(days=3)))
    await add_event(film=film, event_type="release_date", created_at=_on(TODAY, 1))
    await add_event(
        film=film,
        event_type="release_date",
        provenance="catalog",
        occurred_at=_on(TODAY, 1),
        created_at=_on(TODAY, 2),
        subject_key=["GB:wide"],
    )
    await add_event(
        film=film, event_type="trailer", created_at=_on(TODAY, 3), subject_key=["US:wide"]
    )

    _result, mailbox = await send("weekly")

    (envelope,) = mailbox.sent
    assert "  Arrival — Wide release" in envelope.text.splitlines()
    assert "[new]" not in envelope.text and "[moved]" not in envelope.text


# --- the timeline in mail (NEU-1528) -------------------------------------------


async def test_films_rank_for_the_subject_by_their_lead_beat_then_title(
    session, subscriber, make_film, add_event, queue_digest, send
):
    """DC-7: the release date outranks two castings, and the tie between those is the
    casefolded title's. In the body each sits under its own update type, in the feed's
    heading order."""
    user = await subscriber()
    cobra = await make_film(slug="cobra", title="Cobra")
    alpha = await make_film(slug="alpha", title="alpha")
    zed = await make_film(slug="zed", title="Zed")
    for film, event_type in ((cobra, "casting"), (alpha, "casting"), (zed, "release_date")):
        event = await add_event(film=film, event_type=event_type, created_at=NEWER_DAY)
        await queue_digest(user_id=user.id, event_id=event.id)

    _result, mailbox = await send("weekly")

    (envelope,) = mailbox.sent
    assert envelope.subject == "Zed — release date, + 2 more films"
    # The HTML from the Films heading on: the poster strip above it names films in its own
    # (natural title) order.
    html = envelope.html[envelope.html.index(">Films</h4>") :]
    for part in (_timeline(envelope.text), html):
        assert part.index("Zed") < part.index("alpha") < part.index("Cobra")


async def test_the_daily_is_one_timeline_day_per_publication_day_newest_first(
    session, subscriber, make_film, add_event, queue_digest, set_cadence, send
):
    """FB-18: a batch after a missed send spans two days and renders two, each under the
    feed's day heading, and the lines carry no date — the heading says it."""
    user = await subscriber()
    await set_cadence(user.id, "daily")
    heat = await make_film(slug="heat-2", title="Heat 2")
    older = await add_event(
        film=heat, event_type="casting", created_at=OLDER_DAY, summary="Ada joined the cast."
    )
    newer = await add_event(
        film=heat, event_type="trailer", created_at=NEWER_DAY, summary="A trailer landed."
    )
    for event in (older, newer):
        await queue_digest(user_id=user.id, event_id=event.id)

    result, mailbox = await send("daily")

    assert (result.mails_sent, result.sent) == (1, 2)
    text = _timeline(mailbox.sent[0].text)
    assert text.index("Thursday, September 17, 2026") < text.index("A trailer landed.")
    assert text.index("A trailer landed.") < text.index("Tuesday, September 15, 2026")
    assert text.index("Tuesday, September 15, 2026") < text.index("Ada joined the cast.")
    assert "  Ada joined the cast.\n" in text
    assert "17 Sep" not in text


async def test_a_story_covered_card_is_in_the_news_with_its_marker_and_first_outlet(
    session, subscriber, make_film, add_event, queue_digest, send
):
    """The feed's split (`EXISTS(event_story)`, not provenance): a story-covered card is In the
    news, with its beat label, its Unconfirmed marker when rumored and the first source in
    `EventOut.sources` order — newest distinct outlet — linked by its resolved URL. A catalog
    card is Not yet reported, where neither the marker nor "via TMDB" is repeated (FB-20)."""
    user = await subscriber()
    film = await make_film(slug="heat-2", title="Heat 2")
    story = await add_event(
        film=film,
        event_type="casting",
        confidence="rumored",
        created_at=NEWER_DAY,
        summary="Ada is in talks.",
        sources=(
            {
                "source": "Deadline",
                "url": "https://deadline.example/a",
                "published_at": NEWER_DAY - timedelta(days=2),
            },
            {
                "source": "Google News",
                "outlet": "Variety",
                "url": "https://news.google.example/b",
                "resolved_url": "https://variety.example/b",
                "published_at": NEWER_DAY - timedelta(days=1),
            },
        ),
    )
    catalog = await add_event(
        film=film,
        event_type="release_date",
        provenance="catalog",
        confidence="rumored",
        created_at=NEWER_DAY + timedelta(hours=1),
        summary="US wide release date set.",
    )
    for event in (story, catalog):
        await queue_digest(user_id=user.id, event_id=event.id)

    _result, mailbox = await send("weekly")

    (envelope,) = mailbox.sent
    text = _timeline(envelope.text)
    assert text.index("In the news") < text.index("Ada is in talks.")
    assert text.index("Ada is in talks.") < text.index("Not yet reported (unconfirmed)")
    assert (
        "  17 Sep · Casting [unconfirmed] · Ada is in talks.\n"
        "    via Variety — https://variety.example/b\n"
    ) in text
    assert "  17 Sep · US wide release date set.\n" in text
    assert "Deadline" not in text
    assert "via TMDB" not in envelope.text
    assert text.count("[unconfirmed]") == 1
    assert '<a href="https://variety.example/b"' in envelope.html
    assert envelope.html.count(">Unconfirmed</span>") == 1


async def test_a_film_row_is_headed_by_its_parenthetical_and_no_status_line(
    session,
    subscriber,
    make_film,
    add_event,
    add_release_date,
    attach_credits,
    attach_countries,
    queue_digest,
    send,
):
    """The parenthetical from credits, countries and the release year, spelled as
    `filmParenthetical`, linked with the title; the status line is gone (FB-20) — the feed row
    carries none."""
    user = await subscriber()
    heat = await make_film(slug="heat-2", title="Heat 2", release_date=date(2026, 8, 14))
    await attach_credits(heat, crew=[{"id": 77, "name": "Michael Mann", "job": "Director"}])
    await attach_countries(heat, [("US", "United States of America")])
    await add_release_date(film=heat, release_type=3, release_date=_on(date(2026, 10, 2)))
    event = await add_event(film=heat, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)

    _result, mailbox = await send("weekly")

    (envelope,) = mailbox.sent
    url = f"{BASE_URL}/film/{heat.tmdb_id}-heat-2"
    assert f"Heat 2 (USA, Dir: Michael Mann, 2026) — {url}\n" in envelope.text
    assert "(USA, Dir: Michael Mann, 2026)" in envelope.html
    # The timeline only: the film's US date is on the slate above it, as it should be.
    for part in (_timeline(envelope.text), _timeline(envelope.html)):
        assert "Wide release" not in part
        assert "See the film page" not in part


async def test_a_card_three_follows_reached_is_a_line_in_each_of_their_blocks(
    session,
    subscriber,
    make_film,
    add_event,
    attach_credits,
    attach_companies,
    follow,
    queue_digest,
    set_cadence,
    send,
):
    """FB-5, FB-25: a film followed by title whose director and studio are followed too. The
    director's attachment is a line under the film's row in Films *and* under the director's
    row in People; the studio's attachment under the film and under the studio in Studios. Each
    entity row is headed by the entity, linked to its page, and names the film on the line.
    No "Following:" line; two rows queued, two rows sent."""
    user = await subscriber()
    await set_cadence(user.id, "daily")
    heat = await make_film(slug="heat-2", title="Heat 2")
    await attach_credits(heat, crew=[{"id": 900, "name": "A Director", "job": "Director"}])
    await attach_companies(heat, [(711, "A Studio")])
    crew = await add_event(
        film=heat,
        event_type="crew_attached",
        created_at=NEWER_DAY,
        subject_key=[normalize_name("A Director")],
        summary="A Director will direct.",
    )
    studio = await add_event(
        film=heat,
        event_type="company_attached",
        created_at=NEWER_DAY,
        subject_key=[company_subject_token(711)],
        summary="A Studio joins.",
    )
    for event in (crew, studio):
        await queue_digest(user_id=user.id, event_id=event.id)
    await follow(user_id=user.id, entity_type="person", entity_id="900")
    await follow(user_id=user.id, entity_type="company", entity_id="711")

    result, mailbox = await send("daily")

    assert result.sent == 2
    (envelope,) = mailbox.sent
    text = _timeline(envelope.text)
    film_url = f"{BASE_URL}/film/{heat.tmdb_id}-heat-2"
    films, people = text.index("\nFILMS\n"), text.index("\nPEOPLE\n")
    studios = text.index("\nSTUDIOS\n")
    assert films < people < studios
    assert text[films:people].count("A Director will direct.") == 1
    assert text[films:people].count("A Studio joins.") == 1
    assert (
        f"A Director — {BASE_URL}/person/900-a-director\n"
        f"  Heat 2 (Dir: A Director, 2026) · A Director will direct. — {film_url}\n"
    ) in text[people:studios]
    assert (
        f"A Studio — {BASE_URL}/studio/711-a-studio\n"
        f"  Heat 2 (Dir: A Director, 2026) · A Studio joins. — {film_url}\n"
    ) in text[studios:]
    assert f'<a href="{BASE_URL}/person/900-a-director"' in envelope.html
    for part in (envelope.text, envelope.html):
        assert "Following:" not in part


async def test_a_film_reached_only_through_a_studio_is_an_entity_row_and_can_lead(
    session,
    subscriber,
    make_film,
    add_event,
    attach_companies,
    follow,
    queue_digest,
    send,
):
    """FB-22: the lead film is ranked over every reach. A cancellation reaching the reader only
    through a followed studio names the subject, and sits under Studios → Canceled, not under
    Films."""
    user = await subscriber()
    heat = await make_film(slug="heat-2", title="Heat 2")
    zodiac = await make_film(slug="zodiac", title="Zodiac")
    await attach_companies(zodiac, [(711, "A Studio")])
    casting = await add_event(film=heat, event_type="casting", created_at=NEWER_DAY)
    canceled = await add_event(
        film=zodiac,
        event_type="canceled",
        created_at=NEWER_DAY,
        summary="The film has been canceled.",
    )
    await queue_digest(user_id=user.id, event_id=casting.id)
    await queue_digest(user_id=user.id, event_id=canceled.id, follow_title=False)
    await follow(user_id=user.id, entity_type="company", entity_id="711")

    _result, mailbox = await send("weekly")

    (envelope,) = mailbox.sent
    assert envelope.subject == "Zodiac — canceled, + 1 more film"
    text = _timeline(envelope.text)
    studios = text.index("\nSTUDIOS\n")
    assert text.index("Zodiac") > studios
    assert text.index("-- Canceled --", studios) < text.index("The film has been canceled.")


async def test_a_card_no_follow_reaches_any_more_fails_with_the_reason(
    session, subscriber, make_film, add_event, queue_digest, send
):
    """The reader unfollowed between the decision pass and the send: the timeline no longer
    shows the card, so the mail does not, and the row is failed rather than left `queued`."""
    user = await subscriber()
    film = await make_film(slug="heat-2", title="Heat 2")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id, follow_title=False)

    result, mailbox = await send("weekly")

    assert (result.mails_sent, result.failed) == (0, 1)
    assert mailbox.sent == []
    (row,) = await _rows(session)
    assert (row.status, row.error) == ("failed", "no follow reaches the event any more")


async def test_a_now_available_beat_credits_justwatch(
    session, subscriber, make_film, add_event, queue_digest, send
):
    """NR-8: TMDB's condition on the provider data, once at the foot of Now available, in both
    parts."""
    user = await subscriber()
    zodiac = await make_film(slug="zodiac", title="Zodiac")
    heat = await make_film(slug="heat-2", title="Heat 2")
    streaming = await add_event(
        film=zodiac,
        event_type="now_available",
        provenance="catalog",
        created_at=NEWER_DAY,
        summary="Now streaming on Netflix.",
    )
    casting = await add_event(film=heat, event_type="casting", created_at=NEWER_DAY)
    for event in (streaming, casting):
        await queue_digest(user_id=user.id, event_id=event.id)

    _result, mailbox = await send("weekly")

    (envelope,) = mailbox.sent
    for part in (envelope.text, envelope.html):
        assert part.count("Availability from JustWatch") == 1
    text = _timeline(envelope.text)
    assert text.index("Now streaming on Netflix.") < text.index("Availability from JustWatch")
    assert text.index("Availability from JustWatch") < text.index("Heat 2")


async def test_nothing_is_cut_and_every_row_is_sent(
    session, subscriber, make_film, add_event, queue_digest, send
):
    """FB-21: no cap and no overflow line — 25 films, 25 rows in the mail, 25 rows `sent`."""
    user = await subscriber()
    for n in range(25):
        film = await make_film(slug=f"film-{n:02}", title=f"Film {n:02}")
        event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
        await queue_digest(user_id=user.id, event_id=event.id)

    result, mailbox = await send("weekly")

    (envelope,) = mailbox.sent
    assert envelope.subject == "Film 00 — casting, + 24 more films"
    assert "Film 24 (" in envelope.text
    assert "more film" not in _timeline(envelope.text)
    assert result.sent == 25
    assert [row.status for row in await _rows(session)] == ["sent"] * 25


# --- render_digest: the one render path (D-1460.1) -----------------------------


async def test_render_digest_is_the_mail_the_send_delivers_and_marks_nothing(
    session,
    subscriber,
    make_film,
    add_event,
    add_release_date,
    queue_digest,
    watchlist,
    set_cadence,
    send,
    settings,
):
    """With a settings row, so both carry the same unsubscribe token: a rowless user's preview
    differs from their send by exactly the header and footer link (see the DC-10 tests)."""
    user = await subscriber()
    await set_cadence(user.id, "weekly")
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))
    event = await add_event(film=dune, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)

    preview = await render_digest(session, user.id, "weekly", TODAY, settings)

    assert preview is not None
    assert [row.status for row in await _rows(session)] == ["queued"]
    _result, mailbox = await send("weekly")
    (sent,) = mailbox.sent
    assert (preview.subject, preview.text, preview.html, preview.headers) == (
        sent.subject,
        sent.text,
        sent.html,
        sent.headers,
    )


async def test_render_digest_has_nothing_for_a_user_with_nothing(session, subscriber, settings):
    user = await subscriber()

    assert await render_digest(session, user.id, "weekly", TODAY, settings) is None


async def test_render_digest_refuses_an_unknown_user(session, settings):
    with pytest.raises(LookupError):
        await render_digest(session, UUID(int=0), "weekly", TODAY, settings)


async def test_render_digest_ignores_the_gate_that_the_send_still_applies(
    session,
    subscriber,
    make_film,
    add_event,
    add_release_date,
    queue_digest,
    watchlist,
    send,
    settings,
):
    """A lapsed subscriber's mail — slate included — is still there to preview; the send pass
    suppresses it exactly as before."""
    user = await subscriber(entitled_until=LAPSED)
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))
    event = await add_event(film=dune, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)

    preview = await render_digest(session, user.id, "weekly", TODAY, settings)
    result, mailbox = await send("weekly")

    assert preview is not None
    assert preview.subject == "Dune — casting · your slate"
    assert (result.mails_sent, result.suppressed) == (0, 1)
    assert mailbox.sent == []


# --- one-click unsubscribe (DC-10) ---------------------------------------------


async def _token(session, user_id: UUID) -> str | None:
    return await session.scalar(
        select(UserSettings.unsubscribe_token)
        .where(UserSettings.user_id == user_id)
        .execution_options(populate_existing=True)
    )


async def test_the_digest_carries_the_one_click_unsubscribe_headers(
    session, subscriber, make_film, add_event, queue_digest, set_cadence, send
):
    user = await subscriber()
    await set_cadence(user.id, "weekly")
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)
    token = await _token(session, user.id)

    _result, mailbox = await send("weekly")

    (mail,) = mailbox.sent
    link = f"{API_BASE}/digest/unsubscribe/{token}"
    assert dict(mail.headers) == {
        "List-Unsubscribe": f"<{link}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert link in mail.text
    assert f'href="{link}"' in mail.html


async def test_a_rowless_reader_gets_a_settings_row_and_a_token_when_mailed(
    session, subscriber, make_film, add_event, queue_digest, send
):
    """Most weekly readers never opened their settings, so they have no row and no token. The
    send creates the row the settings page would have — on the default cadence, so nothing
    they receive changes — and the mail's header names the token it wrote."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)

    _result, mailbox = await send("weekly")

    row = await session.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user.id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    assert row.digest_cadence == "weekly"
    (mail,) = mailbox.sent
    assert mail.headers["List-Unsubscribe"] == (
        f"<{API_BASE}/digest/unsubscribe/{row.unsubscribe_token}>"
    )


async def test_no_settings_row_is_written_for_a_reader_who_is_not_mailed(
    session, subscriber, make_film, add_event, add_release_date, queue_digest, watchlist, send
):
    """The row is written for a user the pass is *about to mail*, and nobody else: not one the
    gate refuses, and not one with nothing to say (`settings_service`'s rule)."""
    refused = await subscriber("lapsed@example.com", entitled_until=LAPSED)
    quiet = await subscriber("quiet@example.com")
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=refused.id, event_id=event.id)
    await watchlist(user_id=quiet.id, film_id=film.id)
    await add_release_date(film=film, release_date=_on(TODAY + timedelta(days=60)))

    result, mailbox = await send("weekly")

    assert (result.users_considered, result.mails_sent) == (2, 0)
    assert mailbox.sent == []
    assert (await session.scalars(select(UserSettings))).all() == []


async def test_render_digest_writes_no_row_and_so_carries_no_header_for_a_rowless_reader(
    session, subscriber, make_film, add_event, queue_digest, settings
):
    """The preview marks and writes nothing (M3). A rowless reader's preview is therefore the
    mail without its unsubscribe header or footer link — the send adds both."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", created_at=NEWER_DAY)
    await queue_digest(user_id=user.id, event_id=event.id)

    preview = await render_digest(session, user.id, "weekly", TODAY, settings)

    assert preview is not None
    assert dict(preview.headers) == {}
    assert "/digest/unsubscribe/" not in preview.text
    assert await _token(session, user.id) is None


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

    # Still considered — the title follow puts them in the weekly slate's working set — but
    # with nothing queued and nothing dated there is no mail.
    assert (result.users_considered, result.mails_sent) == (1, 0)
    assert mailbox.sent == []


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


async def test_unfollowing_takes_a_film_off_the_slate(
    session, subscriber, make_film, add_release_date, watchlist, send
):
    """EF-14: the slate reads the user's title follows and nothing subtracts from them, so the
    film leaves the weekly mail on the same terms it leaves the calendar — when the follow
    goes."""
    user = await subscriber()
    dune = await make_film(slug="dune", title="Dune")
    await watchlist(user_id=user.id, film_id=dune.id)
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))
    await session.execute(sa_delete(Follow).where(Follow.user_id == user.id))
    await session.commit()

    result, mailbox = await send("weekly")

    assert result.slate_dates == 0
    assert mailbox.sent == []


async def test_a_film_reached_only_through_a_director_follow_is_not_on_the_slate(
    session, subscriber, make_film, add_release_date, attach_credits, send
):
    """The cutover (EF-14): the slate is the user's title follows, so a film they never named
    is not on it however close they are to the person making it. An entity follow delivers that
    person's attachment cards instead (EF-3), which reach the digest's timeline section."""
    user = await subscriber()
    dune = await make_film(slug="dune", title="Dune")
    await attach_credits(dune, crew=[{"id": 900, "name": "A Director", "job": "Director"}])
    session.add(
        Follow(
            user_id=user.id,
            entity_type="person",
            entity_id="900",
            source="manual",
        )
    )
    await session.commit()
    await add_release_date(film=dune, release_date=_on(TODAY + timedelta(days=3)))

    result, _mailbox = await send("weekly")

    assert result.slate_dates == 0


async def test_a_released_film_a_company_follow_reaches_is_not_on_the_slate(
    session, subscriber, make_film, add_release_date, attach_companies, send
):
    """The same rule at the other end of the alert window, which bounded indirect coverage and
    has no indirect coverage left to bound (EF-14)."""
    user = await subscriber()
    zodiac = await make_film(
        slug="zodiac",
        title="Zodiac",
        status="Released",
        release_date=TODAY - timedelta(days=300),
    )
    await attach_companies(zodiac, [(711, "A Studio")])
    session.add(Follow(user_id=user.id, entity_type="company", entity_id="711", source="manual"))
    await session.commit()
    await add_release_date(film=zodiac, release_type=4, release_date=_on(TODAY + timedelta(days=3)))

    result, _mailbox = await send("weekly")

    assert result.slate_dates == 0


async def test_a_title_follow_keeps_a_long_released_film_on_the_slate(
    session, subscriber, make_film, add_release_date, watchlist, send
):
    """The other half of EF-14: a title follow carries no window and no status term, so the
    home-release date of a film that opened last year is exactly what the slate is for."""
    user = await subscriber()
    zodiac = await make_film(
        slug="zodiac",
        title="Zodiac",
        status="Released",
        release_date=TODAY - timedelta(days=300),
    )
    await watchlist(user_id=user.id, film_id=zodiac.id)
    await add_release_date(film=zodiac, release_type=4, release_date=_on(TODAY + timedelta(days=3)))

    result, mailbox = await send("weekly")

    assert result.slate_dates == 1
    assert len(mailbox.sent) == 1


async def test_a_followed_film_with_no_slug_is_not_on_the_slate(
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
        "canceled",
        "cast_removed",
        "casting",
        "collection_attached",
        "collection_removed",
        "company_attached",
        "company_removed",
        "crew_attached",
        "crew_removed",
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
    assert digest_beat_label("cast_removed") == "Cast departure"
    assert digest_beat_label("crew_removed") == "Crew departure"
    assert digest_beat_label("bogus") == "Update"
