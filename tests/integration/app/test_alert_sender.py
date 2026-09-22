"""The alert send pass (NEU-1380): which `queued` rows become mail, what the mail carries, and
what every row's status says afterwards.

The decision pass is `test_notify_pass.py`'s subject, so the backlog here is seeded directly —
a `queued` alert row is the contract between the two passes, and building it by hand is what
lets this file cover the cases a decision pass would never produce in one run: a row whose user
lost their entitlement overnight, a row whose event lost its summary, a row left behind by a
run that failed while sending.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select, update

from upmovies.app.models import Notification
from upmovies.app.services.alert_sender import (
    alert_send_detail,
    beat_label,
    send_queued_alerts,
)
from upmovies.config import get_settings
from upmovies.ingest.runs import create_run
from upmovies.mail import MailError, MailGateway, MessageId, NoopTransport
from upmovies.news.models import Event, EventSummary

GRANTED = datetime(2027, 1, 1, tzinfo=UTC)
LAPSED = datetime(2026, 1, 1, tzinfo=UTC)
VERIFIED = datetime(2026, 1, 1, tzinfo=UTC)
BASE_URL = "https://app.example.test"
IMAGE_BASE = "https://image.tmdb.test/t/p"


@pytest.fixture
def settings():
    """A deployment whose URLs are distinctive, so an assertion that finds one in a mail is
    finding the one this pass built rather than a default that happens to match."""
    return get_settings().model_copy(
        update={
            "public_base_url": BASE_URL,
            "tmdb_image_base": IMAGE_BASE,
            "product_name": "Backlotter",
        }
    )


@pytest.fixture
def subscriber(make_user):
    """The ordinary recipient: verified and holding a live grant."""

    async def _make(email: str = "sub@example.com", **kwargs):
        kwargs.setdefault("entitled_until", GRANTED)
        kwargs.setdefault("email_verified_at", VERIFIED)
        return await make_user(email=email, **kwargs)

    return _make


@pytest.fixture
def queue_alert(session):
    """Put one `queued` alert row in front of the sender."""

    async def _queue(*, user_id: UUID, event_id: UUID, kind: str = "alert", channel: str = "email"):
        row = Notification(
            user_id=user_id, event_id=event_id, kind=kind, channel=channel, status="queued"
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    return _queue


@pytest.fixture
def send(session_factory, settings):
    """Run the pass the way `pipeline_run.run_notify_stage` does — its own run row, its own
    sessions, one gateway over the whole backlog."""

    async def _send(transport=None):
        transport = transport or NoopTransport()
        async with session_factory() as s:
            run_id = await create_run(s, kind="notify")
            await s.commit()
        async with MailGateway(settings, transport=transport) as mailer:
            result = await send_queued_alerts(
                session_factory=session_factory,
                run_id=run_id,
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
    """A provider having a bad minute. `MailError` because that is what the package promises a
    caller catches; the send pass must turn it into `failed` rows, not into a crash."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, envelope):
        self.attempts += 1
        raise MailError("the provider said no")

    async def aclose(self) -> None:
        pass


# --- the ordinary send ---------------------------------------------------------


async def test_a_queued_alert_sends_once_and_the_row_records_it(
    session, subscriber, make_film, add_event, queue_alert, send
):
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", summary="US wide date moved.")
    await queue_alert(user_id=user.id, event_id=event.id)

    result, mailbox = await send()

    assert (result.mails_sent, result.sent, result.failed, result.suppressed) == (1, 1, 0, 0)
    (envelope,) = mailbox.sent
    assert envelope.to == "sub@example.com"
    (row,) = await _rows(session)
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.error is None


async def test_the_mail_carries_the_title_poster_summary_film_link_and_settings_link(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """The spec's template contract, asserted against a real film row rather than a
    hand-written context: this is the only test that proves the *query* fetches what the
    template renders."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune", poster_path="/dune.jpg")
    event = await add_event(film=film, event_type="now_available", summary="Now streaming on Max.")
    await queue_alert(user_id=user.id, event_id=event.id)

    _, mailbox = await send()

    (envelope,) = mailbox.sent
    assert "Dune" in envelope.subject
    for part in (envelope.text, envelope.html):
        assert "Dune" in part
        assert "Now streaming on Max." in part
        assert f"{BASE_URL}/film/{film.tmdb_id}-dune" in part
        assert f"{BASE_URL}/settings" in part
    assert f"{IMAGE_BASE}/w154/dune.jpg" in envelope.html


async def test_several_alerts_for_one_user_ride_on_one_mail(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """The ticket's batching rule (D-31): three films that moved on one day are one mail, and
    all three rows carry that mail's outcome."""
    user = await subscriber()
    for slug, title in (("dune", "Dune"), ("heat-2", "Heat 2"), ("tron", "Tron")):
        film = await make_film(slug=slug, title=title)
        event = await add_event(film=film, event_type="trailer", summary=f"A trailer for {title}.")
        await queue_alert(user_id=user.id, event_id=event.id)

    result, mailbox = await send()

    assert (result.mails_sent, result.sent) == (1, 3)
    assert len(mailbox.sent) == 1
    (envelope,) = mailbox.sent
    assert envelope.subject == "3 updates from your follows"
    for title in ("Dune", "Heat 2", "Tron"):
        assert title in envelope.text
    assert [row.status for row in await _rows(session)] == ["sent"] * 3


async def test_two_users_get_a_mail_each(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """Batching is per user, not per run — the one way to get this wrong is to batch too far."""
    ada = await subscriber("ada@example.com")
    bob = await subscriber("bob@example.com")
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_alert(user_id=ada.id, event_id=event.id)
    await queue_alert(user_id=bob.id, event_id=event.id)

    result, mailbox = await send()

    assert (result.mails_sent, result.sent) == (2, 2)
    assert sorted(e.to for e in mailbox.sent) == ["ada@example.com", "bob@example.com"]


async def test_a_second_run_sends_nothing_because_nothing_is_queued(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """What keeps a nightly slot from re-mailing last night's news: `sent` is not `queued`."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_alert(user_id=user.id, event_id=event.id)
    await send()

    result, mailbox = await send()

    assert (result.users_considered, result.mails_sent) == (0, 0)
    assert mailbox.sent == []


# --- what the pass leaves alone ------------------------------------------------


async def test_digest_rows_and_push_rows_are_not_this_pass_s_work(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """`kind` and `channel` are both in the `where`: the digest is its own pass on its own
    cadence (D-33, NEU-1381), and `push` has no transport at all until D-36."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_alert(user_id=user.id, event_id=event.id, kind="digest")
    await queue_alert(user_id=user.id, event_id=event.id, channel="push")

    result, mailbox = await send()

    assert (result.users_considered, result.mails_sent) == (0, 0)
    assert mailbox.sent == []
    assert {row.status for row in await _rows(session)} == {"queued"}


async def test_a_row_that_is_not_queued_is_never_reconsidered(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """A `suppressed` row is a decision already made, not a backlog item."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    row = await queue_alert(user_id=user.id, event_id=event.id)
    await session.execute(
        update(Notification).where(Notification.id == row.id).values(status="suppressed")
    )
    await session.commit()

    result, mailbox = await send()

    assert result.users_considered == 0
    assert mailbox.sent == []


# --- the access gate, re-read at send time (D-31, D-39) ------------------------


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("email_verified_at", None, "unverified"),
        ("entitled_until", LAPSED, "entitlement lapsed"),
    ],
)
async def test_a_user_who_may_not_be_mailed_gets_a_suppressed_row_and_no_mail(
    session, subscriber, make_film, add_event, queue_alert, send, field, value, why
):
    """The queue survives a failed run, so a row queued when the gate said yes can be sent on a
    night when it says no. Suppression is a row, not an absence — that is the whole point of
    recording it (D-39)."""
    user = await subscriber(**{field: value})
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_alert(user_id=user.id, event_id=event.id)

    result, mailbox = await send()

    assert (result.mails_sent, result.sent, result.suppressed) == (0, 0, 1), why
    assert mailbox.sent == []
    (row,) = await _rows(session)
    assert row.status == "suppressed"
    assert (row.sent_at, row.error) == (None, None)


async def test_one_suppressed_user_does_not_cost_the_next_user_their_mail(
    session, subscriber, make_film, add_event, queue_alert, send
):
    ada = await subscriber("ada@example.com", email_verified_at=None)
    bob = await subscriber("bob@example.com")
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_alert(user_id=ada.id, event_id=event.id)
    await queue_alert(user_id=bob.id, event_id=event.id)

    result, mailbox = await send()

    assert (result.mails_sent, result.sent, result.suppressed) == (1, 1, 1)
    assert [e.to for e in mailbox.sent] == ["bob@example.com"]


# --- failures ------------------------------------------------------------------


async def test_a_provider_failure_marks_every_row_it_carried_failed_with_the_reason(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """A row left `queued` after its mail was attempted would ride along in somebody's next
    batch, so the whole batch takes the outcome — and the error is what a person reading the
    row afterwards actually needs."""
    user = await subscriber()
    for slug in ("dune", "heat-2"):
        film = await make_film(slug=slug, title=slug.title())
        event = await add_event(film=film, event_type="release_date")
        await queue_alert(user_id=user.id, event_id=event.id)

    result, transport = await send(BrokenTransport())

    assert (result.mails_sent, result.sent, result.failed) == (0, 0, 2)
    assert transport.attempts == 1
    rows = await _rows(session)
    assert [row.status for row in rows] == ["failed", "failed"]
    assert all(row.error == "MailError: the provider said no" for row in rows)
    assert all(row.sent_at is None for row in rows)


async def test_an_event_with_no_summary_fails_its_row_rather_than_stalling_it(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """There is no copy to put in a mail, and a row that can never be sent must not sit in a
    backlog that is retried every night with nothing saying so."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", summary=None)
    await queue_alert(user_id=user.id, event_id=event.id)

    result, mailbox = await send()

    assert (result.mails_sent, result.failed) == (0, 1)
    assert mailbox.sent == []
    (row,) = await _rows(session)
    assert (row.status, row.error) == ("failed", "the event has no summary")


async def test_one_summaryless_event_does_not_cost_the_batch_its_other_alerts(
    session, subscriber, make_film, add_event, queue_alert, send
):
    user = await subscriber()
    good = await make_film(slug="dune", title="Dune")
    bad = await make_film(slug="heat-2", title="Heat 2")
    with_summary = await add_event(film=good, event_type="release_date", summary="A date moved.")
    without = await add_event(film=bad, event_type="release_date", summary=None)
    await queue_alert(user_id=user.id, event_id=with_summary.id)
    await queue_alert(user_id=user.id, event_id=without.id)

    result, mailbox = await send()

    assert (result.mails_sent, result.sent, result.failed) == (1, 1, 1)
    (envelope,) = mailbox.sent
    assert "Dune" in envelope.text
    assert "Heat 2" not in envelope.text


async def test_a_superseded_event_is_not_mailed_and_its_row_says_why(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """A correction replaced the card while its row sat in the queue. The decision pass admits
    only `published` events, so this is the queue having outlived that check — and a mail whose
    claim the product has already retracted is worse than no mail."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", summary="A date moved.")
    await queue_alert(user_id=user.id, event_id=event.id)
    await session.execute(update(Event).where(Event.id == event.id).values(status="superseded"))
    await session.commit()

    result, mailbox = await send()

    assert (result.mails_sent, result.failed) == (0, 1)
    assert mailbox.sent == []
    (row,) = await _rows(session)
    assert (row.status, row.error) == ("failed", "the event is no longer published")


async def test_a_user_who_may_not_be_mailed_has_even_their_unsendable_rows_suppressed(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """The gate is the whole answer for that user. Marking their summaryless row `failed` would
    put a row nothing ever intended to send into an operator's failure read."""
    user = await subscriber(email_verified_at=None)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", summary=None)
    await queue_alert(user_id=user.id, event_id=event.id)

    result, mailbox = await send()

    assert (result.failed, result.suppressed) == (0, 1)
    assert mailbox.sent == []
    (row,) = await _rows(session)
    assert (row.status, row.error) == ("suppressed", None)


async def test_a_failed_send_clears_yesterday_s_error_when_it_finally_goes_out(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """`mark` writes `error` and `sent_at` unconditionally, so a row cannot carry a stale error
    beside a fresh `sent_at`. Re-queued by hand, which is what an operator does to a `failed`
    row."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    row = await queue_alert(user_id=user.id, event_id=event.id)
    await send(BrokenTransport())
    # Re-queued in SQL, not through the ORM object this session still holds: the send pass
    # wrote the `failed` status from a session of its own, so the loaded row is stale.
    await session.execute(
        update(Notification).where(Notification.id == row.id).values(status="queued")
    )
    await session.commit()

    await send()

    (row,) = await _rows(session)
    assert (row.status, row.error) == ("sent", None)
    assert row.sent_at is not None


# --- the abort guard: a provider that is down must not go green ----------------


async def test_consecutive_provider_failures_abort_the_pass(
    session, subscriber, make_film, add_event, queue_alert, session_factory, settings
):
    """The failure this guard exists for. A rotated key refuses every send, so without the
    refusals counting the pass would convert the whole backlog into `failed` rows and still
    report success. It stops at the threshold instead, and everything it did not reach is
    still `queued` for a run made after somebody fixed the key."""
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    for n in range(4):
        user = await subscriber(f"user{n}@example.com")
        await queue_alert(user_id=user.id, event_id=event.id)
    transport = BrokenTransport()

    async with session_factory() as s:
        run_id = await create_run(s, kind="notify")
        await s.commit()
    async with MailGateway(settings, transport=transport) as mailer:
        result = await send_queued_alerts(
            session_factory=session_factory,
            run_id=run_id,
            mailer=mailer,
            settings=settings,
            failure_threshold=2,
        )

    assert result.aborted is True
    assert result.abort_error is not None
    assert "provider failures" in result.abort_error
    assert transport.attempts == 2, "the pass kept mailing a provider that had refused twice"
    statuses = sorted(row.status for row in await _rows(session))
    assert statuses == ["failed", "failed", "queued", "queued"]


async def test_a_run_of_failures_that_recovers_does_not_abort(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """The guard counts *consecutive* refusals, so one user's bad minute is not an outage —
    the counter has to reset on the next success or a long backlog would abort on noise."""

    class FlakyTransport:
        """Refuses the first send and accepts every one after it."""

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
    event = await add_event(film=film, event_type="release_date")
    for n in range(3):
        user = await subscriber(f"user{n}@example.com")
        await queue_alert(user_id=user.id, event_id=event.id)

    result, _ = await send(FlakyTransport())

    assert result.aborted is False
    assert (result.sent, result.failed) == (2, 1)


# --- the detail line and the small rules ---------------------------------------


async def test_the_detail_line_reports_mails_rows_and_suppressions(
    session, subscriber, make_film, add_event, queue_alert, send
):
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_alert(user_id=user.id, event_id=event.id)

    result, _ = await send()

    assert alert_send_detail(result) == (
        "alerts: 1 mails to 1 users, 1 sent, 0 failed, 0 suppressed, 0 lost"
    )


def test_every_whitelisted_beat_has_a_name_and_an_unknown_one_still_reads():
    """D-32's three types are the only ones that reach an alert row; a fourth added to the
    decision pass and forgotten here reads plainly rather than failing the batch."""
    assert beat_label("release_date") == "Release date"
    assert beat_label("now_available") == "Now available"
    assert beat_label("trailer") == "New trailer"
    assert beat_label("casting") == "Update"


async def test_the_pass_reads_the_summary_the_ledger_holds_now(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """The copy is fetched at send time, not frozen at decision time: an admin edit between the
    two passes is the version the reader gets (`EventSummary.edited_at`)."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", summary="The first draft.")
    await queue_alert(user_id=user.id, event_id=event.id)
    summary = await session.get(EventSummary, event.id)
    assert summary is not None
    summary.summary = "The corrected copy."
    await session.commit()

    _, mailbox = await send()

    (envelope,) = mailbox.sent
    assert "The corrected copy." in envelope.text
    assert "The first draft." not in envelope.text


async def test_a_film_with_no_poster_still_sends(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """`poster_path` is nullable, and a film without one is not a reason to withhold the news."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune", poster_path=None)
    event = await add_event(film=film, event_type="release_date")
    await queue_alert(user_id=user.id, event_id=event.id)

    result, mailbox = await send()

    assert result.sent == 1
    assert "<img" not in mailbox.sent[0].html


async def test_the_pass_does_not_care_which_event_types_are_queued(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """D-32's whitelist is the decision pass's rule, applied once where the decision is made.
    Re-applying it here would be a second copy free to drift — this pass sends what it is
    given."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting", summary="Someone was cast.")
    await queue_alert(user_id=user.id, event_id=event.id)

    result, mailbox = await send()

    assert result.sent == 1
    assert "Update" in mailbox.sent[0].text


async def test_an_event_deleted_between_the_passes_takes_its_row_with_it(
    session, subscriber, make_film, add_event, queue_alert, send
):
    """`event_id` cascades (`app.models.Notification`), so a deleted event is not a backlog
    item this pass has to have an opinion about."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_alert(user_id=user.id, event_id=event.id)
    await session.delete(await session.get(Event, event.id))
    await session.commit()

    result, mailbox = await send()

    assert (result.users_considered, result.mails_sent) == (0, 0)
    assert mailbox.sent == []
    assert await _rows(session) == []
