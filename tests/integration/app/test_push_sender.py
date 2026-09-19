"""The push send pass (NEU-1387): which `queued` push rows become notifications, what each
notification carries, what happens to a subscription the push service has forgotten, and what
every row's status says afterwards.

The backlog is seeded directly, as in `test_alert_sender.py` and for the same reason: a
`queued` row is the contract between the decision pass and this one, and building it by hand
covers the cases one run of the decision pass would never produce — a user whose grant lapsed
overnight, a row whose event lost its summary, a row left behind by a failed send.

The push service is a recorder (`FakePusher`), because what is under test is this module's
bookkeeping, not `pywebpush`'s encryption (`tests/unit/push/test_gateway.py` owns that seam).
"""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select

from upmovies.app.models import Notification, PushSubscription
from upmovies.app.services.push_sender import (
    NO_SUBSCRIPTIONS,
    push_send_detail,
    send_queued_pushes,
)
from upmovies.config import get_settings
from upmovies.ingest.runs import create_run
from upmovies.push import PushError, PushSubscriptionGone, PushSubscriptionInfo

GRANTED = datetime(2027, 1, 1, tzinfo=UTC)
LAPSED = datetime(2026, 1, 1, tzinfo=UTC)
VERIFIED = datetime(2026, 1, 1, tzinfo=UTC)
BASE_URL = "https://app.example.test"
IMAGE_BASE = "https://image.tmdb.test/t/p"
ENDPOINT = "https://push.example.test/fcm/abc"
OTHER_ENDPOINT = "https://push.example.test/fcm/def"


@pytest.fixture
def settings():
    """A deployment whose URLs are distinctive, so an assertion that finds one in a payload is
    finding the one this pass built."""
    return get_settings().model_copy(
        update={"public_base_url": BASE_URL, "tmdb_image_base": IMAGE_BASE}
    )


class FakePusher:
    """A push service that accepts everything and remembers what it was handed."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, *, subscription: PushSubscriptionInfo, payload: str) -> None:
        self.sent.append((subscription.endpoint, json.loads(payload)))


class GonePusher(FakePusher):
    """A push service that has forgotten the endpoints named in `gone` (404/410)."""

    def __init__(self, *gone: str) -> None:
        super().__init__()
        self.gone = set(gone)

    async def send(self, *, subscription: PushSubscriptionInfo, payload: str) -> None:
        if subscription.endpoint in self.gone:
            raise PushSubscriptionGone("410: the endpoint is gone")
        await super().send(subscription=subscription, payload=payload)


class BrokenPusher:
    """A push service having a bad minute: every send refused, no endpoint retired."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, *, subscription: PushSubscriptionInfo, payload: str) -> None:
        self.attempts += 1
        raise PushError("the push service said no")


@pytest.fixture
def subscriber(make_user):
    """The ordinary recipient: verified and holding a live grant."""

    async def _make(email: str = "sub@example.com", **kwargs):
        kwargs.setdefault("entitled_until", GRANTED)
        kwargs.setdefault("email_verified_at", VERIFIED)
        return await make_user(email=email, **kwargs)

    return _make


@pytest.fixture
def register(session):
    """Register one browser for a user."""

    async def _register(*, user_id: UUID, endpoint: str = ENDPOINT) -> PushSubscription:
        row = PushSubscription(user_id=user_id, endpoint=endpoint, p256dh="key", auth="secret")
        session.add(row)
        await session.commit()
        return row

    return _register


@pytest.fixture
def queue_push(session):
    """Put one `queued` push alert in front of the sender."""

    async def _queue(*, user_id: UUID, event_id: UUID, channel: str = "push", kind: str = "alert"):
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
    sessions."""

    async def _send(pusher=None, *, failure_threshold: int = 10):
        pusher = pusher or FakePusher()
        async with session_factory() as s:
            run_id = await create_run(s, kind="notify")
            await s.commit()
        result = await send_queued_pushes(
            session_factory=session_factory,
            run_id=run_id,
            pusher=pusher,
            settings=settings,
            failure_threshold=failure_threshold,
        )
        return result, pusher

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


async def _subscriptions(session) -> list[PushSubscription]:
    return list(
        (
            await session.execute(
                select(PushSubscription).order_by(PushSubscription.created_at),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )


# --- the ordinary send -----------------------------------------------------------------------


async def test_a_queued_push_is_delivered_and_the_row_records_it(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", summary="US date moved.")
    await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send()

    assert (result.notifications_sent, result.pushes_delivered, result.failed) == (1, 1, 0)
    assert [endpoint for endpoint, _ in pusher.sent] == [ENDPOINT]
    (row,) = await _rows(session)
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.error is None


async def test_the_payload_carries_the_beat_title_summary_link_and_poster(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """The service worker's contract (NEU-1388), asserted against real rows rather than a
    hand-written context: this is the test that proves the query fetches what the notification
    renders."""
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune", poster_path="/dune.jpg")
    event = await add_event(film=film, event_type="now_available", summary="Now streaming on Max.")
    await queue_push(user_id=user.id, event_id=event.id)

    _, pusher = await send()

    ((_, payload),) = pusher.sent
    assert payload["title"] == "Now available: Dune"
    assert payload["body"] == "Now streaming on Max."
    assert payload["url"] == f"{BASE_URL}/film/{film.tmdb_id}-dune"
    assert payload["icon"] == f"{IMAGE_BASE}/w154/dune.jpg"


async def test_a_film_with_no_poster_pushes_without_an_icon(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """No placeholder image: the service worker leaves the icon out, which is one fewer fetch
    on a notification that is already a single line."""
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune", poster_path=None)
    event = await add_event(film=film, event_type="trailer", summary="A teaser landed.")
    await queue_push(user_id=user.id, event_id=event.id)

    _, pusher = await send()

    ((_, payload),) = pusher.sent
    assert payload["icon"] is None


async def test_each_event_is_its_own_notification(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """Where the mail batches three films into one message, push does not: a notification is a
    line on a lock screen, and "3 updates" would cost the reader the thing it is for."""
    user = await subscriber()
    await register(user_id=user.id)
    for slug, title in (("dune", "Dune"), ("heat-2", "Heat 2"), ("tron", "Tron")):
        film = await make_film(slug=slug, title=title)
        event = await add_event(film=film, event_type="trailer", summary=f"A trailer for {title}.")
        await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send()

    assert (result.notifications_sent, result.pushes_delivered) == (3, 3)
    assert {payload["title"] for _, payload in pusher.sent} == {
        "New trailer: Dune",
        "New trailer: Heat 2",
        "New trailer: Tron",
    }
    assert [row.status for row in await _rows(session)] == ["sent"] * 3


async def test_every_browser_of_one_user_gets_the_notification(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """Two devices are two sends by construction — each body is encrypted to its own key — and
    one row, because the question the row answers is whether the user heard about it."""
    user = await subscriber()
    await register(user_id=user.id)
    await register(user_id=user.id, endpoint=OTHER_ENDPOINT)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send()

    assert (result.notifications_sent, result.pushes_delivered) == (1, 2)
    assert {endpoint for endpoint, _ in pusher.sent} == {ENDPOINT, OTHER_ENDPOINT}
    (row,) = await _rows(session)
    assert row.status == "sent"


async def test_two_users_are_two_batches(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    ada = await subscriber("ada@example.com")
    bob = await subscriber("bob@example.com")
    await register(user_id=ada.id)
    await register(user_id=bob.id, endpoint=OTHER_ENDPOINT)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=ada.id, event_id=event.id)
    await queue_push(user_id=bob.id, event_id=event.id)

    result, pusher = await send()

    assert result.users_considered == 2
    assert {endpoint for endpoint, _ in pusher.sent} == {ENDPOINT, OTHER_ENDPOINT}


# --- the channel boundary --------------------------------------------------------------------


async def test_the_email_half_of_the_queue_is_left_alone(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """The two senders read disjoint halves of one queue. A push pass that touched an `email`
    row would either double-send it or strand it."""
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=user.id, event_id=event.id, channel="email")
    await queue_push(user_id=user.id, event_id=event.id, channel="push")

    result, pusher = await send()

    assert result.notifications_sent == 1
    assert len(pusher.sent) == 1
    by_channel = {row.channel: row.status for row in await _rows(session)}
    assert by_channel == {"email": "queued", "push": "sent"}


async def test_a_digest_row_is_not_this_passs_business(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="casting")
    await queue_push(user_id=user.id, event_id=event.id, kind="digest")

    result, pusher = await send()

    assert (result.users_considered, result.notifications_sent) == (0, 0)
    assert pusher.sent == []
    assert [row.status for row in await _rows(session)] == ["queued"]


# --- gone endpoints --------------------------------------------------------------------------


async def test_a_gone_endpoint_is_pruned_and_its_only_row_fails(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """404/410 is the push service saying the registration is over — the one thing other than
    the user unsubscribing that deletes a row (D-40)."""
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=user.id, event_id=event.id)

    result, _ = await send(GonePusher(ENDPOINT))

    assert (result.subscriptions_pruned, result.failed, result.notifications_sent) == (1, 1, 0)
    assert await _subscriptions(session) == []
    (row,) = await _rows(session)
    assert row.status == "failed"
    assert "410" in (row.error or "")


async def test_one_gone_browser_does_not_cost_the_other_the_notification(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """A phone that has been wiped and a laptop that has not: the user heard about it."""
    user = await subscriber()
    await register(user_id=user.id)
    await register(user_id=user.id, endpoint=OTHER_ENDPOINT)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send(GonePusher(ENDPOINT))

    assert (result.notifications_sent, result.pushes_delivered, result.failed) == (1, 1, 0)
    assert result.subscriptions_pruned == 1
    assert [row.endpoint for row in await _subscriptions(session)] == [OTHER_ENDPOINT]
    assert [endpoint for endpoint, _ in pusher.sent] == [OTHER_ENDPOINT]
    (row,) = await _rows(session)
    assert row.status == "sent"


async def test_a_pruned_endpoint_is_not_tried_again_later_in_the_batch(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """The deletion happens inside the batch's session, so the second event in the same run
    does not re-attempt an endpoint this run has already retired."""
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    for _ in range(3):
        event = await add_event(film=film, event_type="trailer")
        await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send(GonePusher(ENDPOINT))

    assert pusher.sent == []
    assert result.subscriptions_pruned == 1
    assert [row.status for row in await _rows(session)] == ["failed"] * 3
    assert [row.error for row in await _rows(session)][1:] == [NO_SUBSCRIPTIONS] * 2


async def test_a_user_whose_browsers_all_went_away_fails_rather_than_waits(
    session, subscriber, make_film, add_event, queue_push, send
):
    """The user unsubscribed between the decision and the send. A row nothing can ever deliver
    must not stay `queued` — a permanently un-sendable row in a nightly backlog is the one
    stall no counter would report."""
    user = await subscriber()
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send()

    assert (result.failed, result.notifications_sent) == (1, 0)
    assert pusher.sent == []
    (row,) = await _rows(session)
    assert (row.status, row.error) == ("failed", NO_SUBSCRIPTIONS)


# --- the access gate, re-read at send time ----------------------------------------------------


@pytest.mark.parametrize(
    ("entitled_until", "verified_at"),
    [(LAPSED, VERIFIED), (GRANTED, None)],
    ids=["lapsed grant", "unverified"],
)
async def test_a_user_who_may_not_be_notified_is_suppressed_not_pushed(
    session,
    make_user,
    make_film,
    add_event,
    register,
    queue_push,
    send,
    entitled_until,
    verified_at,
):
    """The queue outlives the run that wrote it, so "when the decision pass looked" can be
    yesterday (D-39). And the subscription survives, because a lapsed grant is not a dead
    endpoint (D-40)."""
    user = await make_user(
        email="lapsed@example.com", entitled_until=entitled_until, email_verified_at=verified_at
    )
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send()

    assert (result.suppressed, result.notifications_sent) == (1, 0)
    assert pusher.sent == []
    (row,) = await _rows(session)
    assert (row.status, row.error) == ("suppressed", None)
    assert len(await _subscriptions(session)) == 1


# --- rows nothing can send --------------------------------------------------------------------


async def test_an_event_that_is_no_longer_published_fails_with_its_reason(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """A correction superseded the card while the row sat in the queue. Pushing a claim the
    product has already replaced is worse than pushing nothing."""
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", status="superseded")
    await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send()

    assert (result.failed, result.notifications_sent) == (1, 0)
    assert pusher.sent == []
    (row,) = await _rows(session)
    assert (row.status, row.error) == ("failed", "the event is no longer published")


async def test_an_event_with_no_summary_fails_with_its_reason(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", summary=None)
    await queue_push(user_id=user.id, event_id=event.id)

    result, _ = await send()

    assert result.failed == 1
    (row,) = await _rows(session)
    assert (row.status, row.error) == ("failed", "the event has no summary")


async def test_an_unsendable_row_of_a_suppressed_user_is_suppressed_not_failed(
    session, make_user, make_film, add_event, register, queue_push, send
):
    """ "We may not notify you" is the whole answer for that user: marking their summaryless row
    `failed` would put a row in an operator's failure read that nothing ever intended to
    send."""
    user = await make_user(email="lapsed@example.com", entitled_until=LAPSED)
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date", summary=None)
    await queue_push(user_id=user.id, event_id=event.id)

    result, _ = await send()

    assert (result.suppressed, result.failed) == (1, 0)
    (row,) = await _rows(session)
    assert row.status == "suppressed"


# --- a push service having a bad run ----------------------------------------------------------


async def test_a_refused_push_fails_the_row_and_keeps_the_subscription(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """A 500 or a timeout is a statement about this minute, not about the registration."""
    user = await subscriber()
    await register(user_id=user.id)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=user.id, event_id=event.id)

    result, _ = await send(BrokenPusher())

    assert (result.failed, result.subscriptions_pruned) == (1, 0)
    (row,) = await _rows(session)
    assert row.status == "failed"
    assert "the push service said no" in (row.error or "")
    assert len(await _subscriptions(session)) == 1


async def test_consecutive_refusals_abort_the_pass_and_leave_the_rest_queued(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """A push service that is refusing everything is one outage, not N faults. The pass stops
    within the threshold and fails the run, so the loss is bounded by the threshold rather than
    by the size of the backlog."""
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    for index in range(5):
        user = await subscriber(f"sub{index}@example.com")
        await register(user_id=user.id, endpoint=f"{ENDPOINT}/{index}")
        await queue_push(user_id=user.id, event_id=event.id)

    result, pusher = await send(BrokenPusher(), failure_threshold=2)

    assert result.aborted
    assert result.abort_error is not None
    assert pusher.attempts == 2
    statuses = [row.status for row in await _rows(session)]
    assert statuses.count("failed") == 2
    assert statuses.count("queued") == 3


async def test_a_gone_endpoint_does_not_count_toward_the_abort_guard(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    """Pruning is a normal, expected event and proves nothing about the next user — where a
    refusal is evidence about the run."""
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    endpoints = []
    for index in range(4):
        user = await subscriber(f"sub{index}@example.com")
        endpoint = f"{ENDPOINT}/{index}"
        endpoints.append(endpoint)
        await register(user_id=user.id, endpoint=endpoint)
        await queue_push(user_id=user.id, event_id=event.id)

    result, _ = await send(GonePusher(*endpoints), failure_threshold=2)

    assert not result.aborted
    assert (result.subscriptions_pruned, result.failed) == (4, 4)


# --- the detail line ---------------------------------------------------------------------------


async def test_the_detail_line_reports_what_the_pass_did(
    session, subscriber, make_film, add_event, register, queue_push, send
):
    user = await subscriber()
    await register(user_id=user.id)
    await register(user_id=user.id, endpoint=OTHER_ENDPOINT)
    film = await make_film(slug="dune", title="Dune")
    event = await add_event(film=film, event_type="release_date")
    await queue_push(user_id=user.id, event_id=event.id)

    result, _ = await send()

    line = push_send_detail(result)
    assert "push: 1 notifications to 1 users" in line
    assert "2 delivered" in line
    assert "0 pruned" in line
