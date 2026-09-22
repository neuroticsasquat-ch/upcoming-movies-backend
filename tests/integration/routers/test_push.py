"""`/me/push` (D-36): registering a browser for notifications, and the key it subscribes with —
behind the entitlement gate (D-39).

Every test runs against a deployment that *has* a VAPID keypair, because that is the state the
routes are for; the last section is the one that takes it away."""

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from tests.fixtures.users import ENTITLED_UNTIL, _build_authed_client
from upmovies.app.models import PushSubscription
from upmovies.config import Settings, get_settings
from upmovies.main import app

PUBLIC_KEY = "BJ_test_application_server_key"
ENDPOINT = "https://push.example.test/fcm/abc123"
OTHER_ENDPOINT = "https://push.example.test/fcm/def456"


def _subscribe_body(endpoint: str = ENDPOINT) -> dict:
    """What `PushSubscription.toJSON()` gives a client in the browser, posted unmodified."""
    return {"endpoint": endpoint, "keys": {"p256dh": "p256dh-value", "auth": "auth-value"}}


@pytest.fixture
def configured() -> Iterator[Settings]:
    """A deployment holding a VAPID keypair. The rest of `Settings` still comes from the
    environment, so the app is otherwise the one every other route test runs against."""
    settings = Settings(  # type: ignore[call-arg]
        VAPID_PUBLIC_KEY=PUBLIC_KEY,
        VAPID_PRIVATE_KEY="a-private-key",
        VAPID_SUBJECT="mailto:ops@example.test",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    yield settings
    app.dependency_overrides.pop(get_settings, None)


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


# --- the gate ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", ""), ("DELETE", ""), ("GET", "/vapid-public-key")],
)
async def test_every_verb_requires_auth(client, configured, method, path):
    r = await client.request(method, f"/me/push{path}", json=_body(method))
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", ""), ("DELETE", ""), ("GET", "/vapid-public-key")],
)
async def test_every_verb_is_403_for_an_unentitled_user(
    authed_client, session, configured, method, path
):
    """Push is subscriber functionality (D-39), and each verb is named so that a refactor
    dropping the gate from one of them fails here rather than shipping."""
    r = await authed_client.request(method, f"/me/push{path}", json=_body(method))
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"
    assert await _subscriptions(session) == []


def _body(method: str) -> dict | None:
    if method == "POST":
        return _subscribe_body()
    if method == "DELETE":
        return {"endpoint": ENDPOINT}
    return None


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_writes_require_the_csrf_header(entitled_client, configured, method):
    del entitled_client.headers["X-CSRF-Token"]
    r = await entitled_client.request(method, "/me/push", json=_body(method))
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"


# --- subscribing ---------------------------------------------------------------------------


async def test_subscribing_stores_the_browsers_registration(entitled_client, session, configured):
    r = await entitled_client.post(
        "/me/push", json=_subscribe_body(), headers={"User-Agent": "Firefox/141.0"}
    )
    assert r.status_code == 204

    (row,) = await _subscriptions(session)
    assert (row.user_id, row.endpoint) == (entitled_client.user.id, ENDPOINT)
    assert (row.p256dh, row.auth) == ("p256dh-value", "auth-value")
    assert row.user_agent == "Firefox/141.0"


async def test_subscribing_twice_keeps_one_row_and_refreshes_the_keys(
    entitled_client, session, configured
):
    """A browser re-registers after a service-worker update with the same endpoint and fresh
    key material. One row, the new keys — a second row would double every notification, and
    the old `p256dh` would encrypt to a key the browser can no longer read."""
    await entitled_client.post("/me/push", json=_subscribe_body())
    refreshed = _subscribe_body() | {"keys": {"p256dh": "new-p256dh", "auth": "new-auth"}}

    r = await entitled_client.post("/me/push", json=refreshed)
    assert r.status_code == 204

    (row,) = await _subscriptions(session)
    assert (row.p256dh, row.auth) == ("new-p256dh", "new-auth")


async def test_two_browsers_of_one_user_are_two_rows(entitled_client, session, configured):
    await entitled_client.post("/me/push", json=_subscribe_body())
    await entitled_client.post("/me/push", json=_subscribe_body(OTHER_ENDPOINT))

    rows = await _subscriptions(session)
    assert {row.endpoint for row in rows} == {ENDPOINT, OTHER_ENDPOINT}


async def test_a_shared_browser_moves_the_registration_to_the_second_user(
    entitled_client, session, configured, make_user
):
    """One browser profile is one endpoint. When a second account subscribes from it the row
    has to move: two rows would push the first user's alerts to whoever is signed in
    now, and a notification is read on a lock screen by whoever is holding the phone."""
    await entitled_client.post("/me/push", json=_subscribe_body())
    other = await make_user(email="other@example.com", entitled_until=ENTITLED_UNTIL)

    async with await _build_authed_client(session, other) as other_client:
        r = await other_client.post("/me/push", json=_subscribe_body())
    assert r.status_code == 204

    (row,) = await _subscriptions(session)
    assert row.user_id == other.id


async def test_a_body_that_is_not_a_browser_subscription_is_refused(entitled_client, configured):
    r = await entitled_client.post("/me/push", json={"endpoint": ENDPOINT})
    assert r.status_code == 422


async def test_a_user_agent_longer_than_the_column_wants_is_truncated(
    entitled_client, session, configured
):
    """The header is caller-controlled and the value is a label for a device list, not
    evidence."""
    r = await entitled_client.post(
        "/me/push", json=_subscribe_body(), headers={"User-Agent": "U" * 900}
    )
    assert r.status_code == 204

    (row,) = await _subscriptions(session)
    assert row.user_agent is not None
    assert len(row.user_agent) == 512


# --- unsubscribing -------------------------------------------------------------------------


async def test_unsubscribing_removes_that_browser_only(entitled_client, session, configured):
    await entitled_client.post("/me/push", json=_subscribe_body())
    await entitled_client.post("/me/push", json=_subscribe_body(OTHER_ENDPOINT))

    r = await entitled_client.request("DELETE", "/me/push", json={"endpoint": ENDPOINT})
    assert r.status_code == 204

    assert [row.endpoint for row in await _subscriptions(session)] == [OTHER_ENDPOINT]


async def test_unsubscribing_something_unregistered_is_still_204(entitled_client, configured):
    """The client has just torn down its own registration and legitimately does not know
    whether we still held it."""
    r = await entitled_client.request(
        "DELETE", "/me/push", json={"endpoint": "https://push.example.test/never-registered"}
    )
    assert r.status_code == 204


async def test_one_user_cannot_unsubscribe_anothers_browser(
    entitled_client, session, configured, make_user
):
    """An endpoint is long and unguessable, but it is still a string a client sends us."""
    await entitled_client.post("/me/push", json=_subscribe_body())
    other = await make_user(email="other@example.com", entitled_until=ENTITLED_UNTIL)

    async with await _build_authed_client(session, other) as other_client:
        r = await other_client.request("DELETE", "/me/push", json={"endpoint": ENDPOINT})
    assert r.status_code == 204

    (row,) = await _subscriptions(session)
    assert row.user_id == entitled_client.user.id


# --- the application server key --------------------------------------------------------------


async def test_the_public_key_is_served_to_an_entitled_user(entitled_client, configured):
    r = await entitled_client.get("/me/push/vapid-public-key")
    assert r.status_code == 200
    assert r.json() == {"public_key": PUBLIC_KEY}


# --- a deployment with no keypair ------------------------------------------------------------


async def test_the_key_route_says_push_is_unavailable_when_unconfigured(entitled_client):
    """No override, so the suite's own environment answers — which carries no VAPID keys."""
    r = await entitled_client.get("/me/push/vapid-public-key")
    assert r.status_code == 503
    assert r.json()["detail"] == "push_unavailable"


async def test_subscribing_is_refused_when_unconfigured(entitled_client, session):
    """Taking a registration this deployment can never push to would leave a row that fails
    the *next* notify run's boot check (`assert_push_sendable`)."""
    r = await entitled_client.post("/me/push", json=_subscribe_body())
    assert r.status_code == 503
    assert r.json()["detail"] == "push_unavailable"
    assert await _subscriptions(session) == []


async def test_unsubscribing_still_works_when_unconfigured(entitled_client, session, make_user):
    """Deliberately outside the guard: a user must always be able to unregister a browser,
    including from a deployment whose keys have been removed since they subscribed."""
    session.add(
        PushSubscription(user_id=entitled_client.user.id, endpoint=ENDPOINT, p256dh="k", auth="a")
    )
    await session.commit()

    r = await entitled_client.request("DELETE", "/me/push", json={"endpoint": ENDPOINT})
    assert r.status_code == 204
    assert await _subscriptions(session) == []


async def test_subscribing_is_refused_when_only_the_subject_is_wrong(entitled_client, session):
    """A keypair with a malformed `VAPID_SUBJECT` would otherwise take subscriptions all day
    and then be refused by every push service. The route asks the same question the notify
    slot boots on, so the two ends cannot disagree about whether push is configured."""
    settings = Settings(  # type: ignore[call-arg]
        VAPID_PUBLIC_KEY=PUBLIC_KEY,
        VAPID_PRIVATE_KEY="a-private-key",
        VAPID_SUBJECT="ops@example.test",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        r = await entitled_client.post("/me/push", json=_subscribe_body())
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert r.status_code == 503
    assert r.json()["detail"] == "push_unavailable"
    assert await _subscriptions(session) == []
