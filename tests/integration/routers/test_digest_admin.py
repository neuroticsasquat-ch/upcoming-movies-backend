"""`/admin/digest/preview` and `/admin/digest/test` (NEU-1464, DC-11): any user's digest,
rendered through the send's own `render_digest`, shown to or mailed to the calling admin — and
never a row marked.

What the mail *says* is `test_digest_sender.py`'s subject; here the question is only what the
routes wrap around it: who may ask, where the mail goes, and that the queue is left alone."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from upmovies.app.models import Follow, Notification, UserSettings
from upmovies.config import get_settings
from upmovies.mail import MailError, MailGateway, MessageId
from upmovies.main import app

TODAY = date(2026, 9, 18)
"""A Friday — off the default `SLATE_WEEKDAY`, so a daily render on it carries no slate."""
SLATE_DAY = date(2026, 9, 17)
"""A Thursday, the default `SLATE_WEEKDAY` (DC-2)."""
GRANTED = datetime(2099, 1, 1, tzinfo=UTC)
LAPSED = datetime(2026, 1, 1, tzinfo=UTC)
VERIFIED = datetime(2026, 1, 1, tzinfo=UTC)
PUBLISHED = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
TOKEN = "unsub-the-users-own"

PREVIEW = "/admin/digest/preview"
TEST = "/admin/digest/test"


@pytest.fixture
def subscriber(session, make_user):
    """A reader with a settings row on `cadence`, so they hold an unsubscribe token the
    test-send must keep out of the admin's inbox."""

    async def _make(
        *,
        cadence: str = "weekly",
        email: str = "reader@example.com",
        entitled_until: datetime = GRANTED,
    ):
        user = await make_user(
            email=email, entitled_until=entitled_until, email_verified_at=VERIFIED
        )
        session.add(
            UserSettings(
                user_id=user.id,
                digest_cadence=cadence,
                ical_token="ical-tok",
                unsubscribe_token=TOKEN,
            )
        )
        await session.commit()
        return user

    return _make


@pytest.fixture
def queued(session, make_film, add_event):
    """One `queued` digest row on a film called Dune — the whole backlog most tests need."""

    async def _queue(user_id: UUID) -> Notification:
        dune = await make_film(slug="dune", title="Dune")
        event = await add_event(film=dune, event_type="casting", created_at=PUBLISHED)
        row = Notification(
            user_id=user_id, event_id=event.id, kind="digest", channel="email", status="queued"
        )
        session.add(row)
        await session.commit()
        return row

    return _queue


async def _statuses(session) -> list[str]:
    rows = await session.execute(
        select(Notification.status).execution_options(populate_existing=True)
    )
    return list(rows.scalars())


def _preview_params(user: UUID, **overrides: str) -> dict[str, str]:
    params = {"user_id": str(user), "cadence": "weekly", "today": TODAY.isoformat()}
    params.update(overrides)
    return params


# --- who may ask ----------------------------------------------------------------------------


async def test_preview_requires_auth(client):
    r = await client.get(PREVIEW, params=_preview_params(UUID(int=0)))
    assert r.status_code == 401


async def test_preview_forbidden_for_non_admin(authed_client):
    r = await authed_client.get(PREVIEW, params=_preview_params(authed_client.user.id))
    assert r.status_code == 403


async def test_test_send_forbidden_for_non_admin(authed_client):
    r = await authed_client.post(
        TEST, json={"user_id": str(authed_client.user.id), "cadence": "weekly"}
    )
    assert r.status_code == 403


async def test_test_send_requires_csrf(admin_authed_client, subscriber):
    user = await subscriber()
    del admin_authed_client.headers["X-CSRF-Token"]
    r = await admin_authed_client.post(TEST, json={"user_id": str(user.id), "cadence": "weekly"})
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"


# --- the preview ----------------------------------------------------------------------------


async def test_preview_html_is_the_mail_and_marks_nothing(
    admin_authed_client, session, subscriber, queued
):
    user = await subscriber()
    await queued(user.id)

    r = await admin_authed_client.get(PREVIEW, params=_preview_params(user.id, format="html"))

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Dune" in r.text
    assert "<a " in r.text
    assert await _statuses(session) == ["queued"]


async def test_preview_text_is_the_plain_part(admin_authed_client, session, subscriber, queued):
    user = await subscriber()
    await queued(user.id)

    r = await admin_authed_client.get(PREVIEW, params=_preview_params(user.id, format="text"))

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "Dune" in r.text
    assert "<a" not in r.text
    assert await _statuses(session) == ["queued"]


async def test_preview_defaults_to_html(admin_authed_client, subscriber, queued):
    user = await subscriber()
    await queued(user.id)

    r = await admin_authed_client.get(PREVIEW, params=_preview_params(user.id))

    assert r.headers["content-type"].startswith("text/html")


async def test_preview_of_nothing_says_so(admin_authed_client, subscriber):
    user = await subscriber()

    r = await admin_authed_client.get(PREVIEW, params=_preview_params(user.id))

    assert r.status_code == 200
    assert r.text == "Nothing to send."


async def test_preview_ignores_the_gate(admin_authed_client, session, subscriber, queued):
    """A lapsed reader's rows would be `suppressed` by the slot; the preview still shows the
    mail they would have got, and leaves the rows where they are."""
    user = await subscriber(entitled_until=LAPSED)
    await queued(user.id)

    r = await admin_authed_client.get(PREVIEW, params=_preview_params(user.id))

    assert r.status_code == 200
    assert "Dune" in r.text
    assert await _statuses(session) == ["queued"]


async def test_preview_date_drives_the_daily_slate_day(
    admin_authed_client, session, subscriber, make_film, add_release_date
):
    user = await subscriber(cadence="daily")
    arrival = await make_film(slug="arrival", title="Arrival")
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(arrival.id), source="manual")
    )
    await session.commit()
    on = SLATE_DAY + timedelta(days=3)
    await add_release_date(
        film=arrival, release_date=datetime(on.year, on.month, on.day, 12, tzinfo=UTC)
    )

    on_the_day = await admin_authed_client.get(
        PREVIEW, params=_preview_params(user.id, cadence="daily", today=SLATE_DAY.isoformat())
    )
    off_the_day = await admin_authed_client.get(
        PREVIEW, params=_preview_params(user.id, cadence="daily", today=TODAY.isoformat())
    )

    assert "Arrival" in on_the_day.text
    assert off_the_day.text == "Nothing to send."


async def test_preview_unknown_user_is_404(admin_authed_client):
    r = await admin_authed_client.get(PREVIEW, params=_preview_params(UUID(int=0)))
    assert r.status_code == 404
    assert r.json()["detail"] == "user_not_found"


@pytest.mark.parametrize(
    "override",
    [
        {"cadence": "off"},
        {"cadence": "monthly"},
        {"format": "pdf"},
        {"today": "2026-13-01"},
        {"user_id": "not-a-uuid"},
    ],
)
async def test_preview_rejects_bad_parameters(admin_authed_client, subscriber, override):
    user = await subscriber()
    r = await admin_authed_client.get(PREVIEW, params=_preview_params(user.id, **override))
    assert r.status_code == 422


# --- the test-send --------------------------------------------------------------------------


async def test_test_send_goes_to_the_admin_not_the_user(
    admin_authed_client, session, subscriber, queued, mailbox
):
    user = await subscriber()
    await queued(user.id)

    r = await admin_authed_client.post(
        TEST, json={"user_id": str(user.id), "cadence": "weekly", "today": TODAY.isoformat()}
    )

    assert r.status_code == 202
    (sent,) = mailbox.sent
    assert set(r.json()) == {"message_id"}
    assert r.json()["message_id"].startswith("noop-")
    assert sent.to == "admin@example.com"
    assert sent.subject.startswith("[test for reader@example.com] ")
    assert "Dune" in sent.text
    assert await _statuses(session) == ["queued"]


async def test_test_send_carries_no_unsubscribe(admin_authed_client, subscriber, queued, mailbox):
    """Neither the header nor the footer link: the token turns the *user's* digest off, and a
    copy of it in the admin's inbox is one link-scanner prefetch away from doing so."""
    user = await subscriber()
    await queued(user.id)

    await admin_authed_client.post(
        TEST, json={"user_id": str(user.id), "cadence": "weekly", "today": TODAY.isoformat()}
    )

    (sent,) = mailbox.sent
    assert dict(sent.headers) == {}
    assert TOKEN not in sent.text
    assert TOKEN not in sent.html


async def test_test_send_ignores_the_gate(
    admin_authed_client, session, subscriber, queued, mailbox
):
    user = await subscriber(entitled_until=LAPSED)
    await queued(user.id)

    r = await admin_authed_client.post(
        TEST, json={"user_id": str(user.id), "cadence": "weekly", "today": TODAY.isoformat()}
    )

    assert r.status_code == 202
    assert len(mailbox.sent) == 1
    assert await _statuses(session) == ["queued"]


async def test_test_send_of_nothing_is_409(admin_authed_client, subscriber, mailbox):
    user = await subscriber()

    r = await admin_authed_client.post(TEST, json={"user_id": str(user.id), "cadence": "weekly"})

    assert r.status_code == 409
    assert r.json()["detail"] == "nothing_to_send"
    assert mailbox.sent == []


async def test_test_send_unknown_user_is_404(admin_authed_client, mailbox):
    r = await admin_authed_client.post(
        TEST, json={"user_id": str(UUID(int=0)), "cadence": "weekly"}
    )
    assert r.status_code == 404
    assert mailbox.sent == []


@pytest.mark.parametrize(
    "body",
    [
        {"cadence": "off"},
        {"cadence": "weekly", "today": "yesterday"},
        {"cadence": "weekly", "user_id": "not-a-uuid"},
    ],
)
async def test_test_send_rejects_bad_bodies(admin_authed_client, subscriber, body):
    user = await subscriber()
    r = await admin_authed_client.post(TEST, json={"user_id": str(user.id), **body})
    assert r.status_code == 422


class _RefusingTransport:
    async def send(self, envelope) -> MessageId:
        raise MailError("provider said no")

    async def aclose(self) -> None:
        return None


async def test_test_send_provider_refusal_is_502(admin_authed_client, session, subscriber, queued):
    user = await subscriber()
    await queued(user.id)
    app.state.mailer = MailGateway(get_settings(), transport=_RefusingTransport())

    r = await admin_authed_client.post(
        TEST, json={"user_id": str(user.id), "cadence": "weekly", "today": TODAY.isoformat()}
    )

    assert r.status_code == 502
    assert r.json()["detail"] == "mail_failed"
    assert await _statuses(session) == ["queued"]
