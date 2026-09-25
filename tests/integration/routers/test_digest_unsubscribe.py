"""`/digest/unsubscribe/{token}` (NEU-1463, DC-10): the digest's one-click unsubscribe.

Public: the `client` fixture here carries no session and no CSRF header, which is the whole of
what a mailbox provider's one-click POST carries too."""

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from upmovies.app import tokens
from upmovies.app.models import UserSettings
from upmovies.app.rate_limit import MemoryStore, get_rate_limit_store
from upmovies.config import Settings, get_settings
from upmovies.main import app

PUBLIC_BASE = "https://app.example.test"


@pytest.fixture
def settings_row(session, make_user):
    """A user with a settings row on `cadence`; returns the row's unsubscribe token."""

    async def _make(
        *, cadence: str = "weekly", email: str = "reader@example.com", entitled_until=None
    ) -> tuple[UserSettings, str]:
        user = await make_user(email=email, entitled_until=entitled_until)
        token = tokens.new_unsubscribe_token()
        row = UserSettings(
            user_id=user.id,
            digest_cadence=cadence,
            ical_token=tokens.new_ical_token(),
            unsubscribe_token=token,
        )
        session.add(row)
        await session.commit()
        return row, token

    return _make


@pytest.fixture
def public_base() -> Iterator[Settings]:
    settings = get_settings().model_copy(update={"public_base_url": PUBLIC_BASE})
    app.dependency_overrides[get_settings] = lambda: settings
    yield settings
    app.dependency_overrides.pop(get_settings, None)


async def _cadence(session, row: UserSettings) -> str:
    await session.refresh(row)
    return row.digest_cadence


async def test_the_one_click_post_turns_the_digest_off(client, session, settings_row):
    row, token = await settings_row(cadence="daily")

    r = await client.post(
        f"/digest/unsubscribe/{token}",
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert r.status_code == 204
    assert r.content == b""
    assert await _cadence(session, row) == "off"


async def test_the_post_is_idempotent(client, session, settings_row):
    """A mailbox provider may POST more than once; the second is the same answer, and it does
    not touch a row that is already off."""
    row, token = await settings_row(cadence="off")
    await session.refresh(row)
    updated_at = row.updated_at

    first = await client.post(f"/digest/unsubscribe/{token}")
    second = await client.post(f"/digest/unsubscribe/{token}")

    assert (first.status_code, second.status_code) == (204, 204)
    assert await _cadence(session, row) == "off"
    assert row.updated_at == updated_at


async def test_the_link_turns_the_digest_off_and_lands_on_the_settings_notice(
    client, session, settings_row, public_base
):
    """For a client that opens the link rather than posting it — and for the footer's
    "unsubscribe" word. The settings page reads `?digest=off` and confirms it (NEU-1466)."""
    row, token = await settings_row(cadence="weekly")

    r = await client.get(f"/digest/unsubscribe/{token}", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == f"{PUBLIC_BASE}/me/settings?digest=off"
    assert await _cadence(session, row) == "off"


@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_an_unknown_token_is_404_and_changes_nothing(client, session, settings_row, method):
    row, _token = await settings_row(cadence="weekly")

    r = await client.request(method, "/digest/unsubscribe/not-a-token", follow_redirects=False)

    assert r.status_code == 404
    assert await _cadence(session, row) == "weekly"


async def test_an_unentitled_reader_can_still_turn_the_digest_off(client, session, settings_row):
    """No entitlement check: stopping a mail is never gated (DC-10)."""
    row, token = await settings_row(cadence="weekly", entitled_until=None)

    r = await client.post(f"/digest/unsubscribe/{token}")

    assert r.status_code == 204
    assert await _cadence(session, row) == "off"


async def test_a_token_turns_off_only_its_own_readers_digest(client, session, settings_row):
    mine, token = await settings_row(cadence="weekly", email="me@example.com")
    theirs, _ = await settings_row(cadence="weekly", email="them@example.com")

    await client.post(f"/digest/unsubscribe/{token}")

    assert await _cadence(session, mine) == "off"
    assert await _cadence(session, theirs) == "weekly"


async def test_the_routes_are_rate_limited_on_their_own_bucket(client, settings_row):
    """The suite runs with the limiter off; this turns it on with a two-request bucket. Both
    methods draw on the same `digest_unsubscribe` bucket."""
    _row, token = await settings_row()
    settings = get_settings().model_copy(
        update={"rate_limit_enabled": True, "rate_limit_digest_unsubscribe": "2/1"}
    )
    store = MemoryStore(clock=lambda: 1000.0)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_rate_limit_store] = lambda: store
    try:
        first = await client.post(f"/digest/unsubscribe/{token}")
        second = await client.get(f"/digest/unsubscribe/{token}", follow_redirects=False)
        third = await client.post(f"/digest/unsubscribe/{token}")
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_rate_limit_store, None)

    assert (first.status_code, second.status_code, third.status_code) == (204, 302, 429)
    assert third.json()["bucket"] == "digest_unsubscribe"


async def test_the_settings_payload_never_carries_the_unsubscribe_token(entitled_client, session):
    """It travels in every digest's headers and can only stop a mail; the settings page has
    no use for it, so it is not part of `/me/settings`."""
    r = await entitled_client.get("/me/settings")

    assert r.status_code == 200
    token = await session.scalar(select(UserSettings.unsubscribe_token))
    assert token
    assert "unsubscribe_token" not in r.json()
    assert token not in r.text
