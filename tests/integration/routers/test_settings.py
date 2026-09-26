"""`/me/settings` (D-33, D-34): the digest cadence and the calendar token, behind the
entitlement gate (D-39)."""

import pytest
from sqlalchemy import select

from tests.fixtures.users import ENTITLED_UNTIL, _build_authed_client
from upmovies.app.models import UserSettings

# --- the gate ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", ""), ("PATCH", ""), ("POST", "/ical-token/rotate")],
)
async def test_every_verb_requires_auth(client, method, path):
    r = await client.request(method, f"/me/settings{path}", json=_body(method))
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", ""), ("PATCH", ""), ("POST", "/ical-token/rotate")],
)
async def test_every_verb_is_403_for_an_unentitled_user(authed_client, session, method, path):
    # `authed_client`'s user has `entitled_until` NULL — the state every signup starts in
    # (D-37). Each verb is named so that a refactor that drops the gate from one of them fails
    # here rather than shipping.
    r = await authed_client.request(method, f"/me/settings{path}", json=_body(method))
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"

    # And the refusal wrote nothing: the lazily created row is a subscriber artefact, so an
    # unentitled account must not acquire one (and with it a working calendar URL) by asking.
    rows = (await session.execute(select(UserSettings))).scalars().all()
    assert rows == []


def _body(method: str) -> dict | None:
    return {"digest_cadence": "daily"} if method == "PATCH" else None


# --- lazy creation -------------------------------------------------------------------------


async def test_first_read_creates_the_row_with_the_documented_defaults(entitled_client, session):
    r = await entitled_client.get("/me/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["digest_cadence"] == "weekly"  # D-33
    assert "alert_stores" not in body  # ADR-0021: no per-beat preference
    assert body["ical_token"]

    rows = (await session.execute(select(UserSettings))).scalars().all()
    assert [row.user_id for row in rows] == [entitled_client.user.id]


async def test_reading_twice_returns_one_row_with_a_stable_token(entitled_client, session):
    first = await entitled_client.get("/me/settings")
    again = await entitled_client.get("/me/settings")

    assert again.json() == first.json()
    rows = (await session.execute(select(UserSettings))).scalars().all()
    assert len(rows) == 1


async def test_two_users_get_two_rows_with_different_tokens(entitled_client, session, make_user):
    other = await make_user(email="other@example.com", entitled_until=ENTITLED_UNTIL)
    async with await _build_authed_client(session, other) as other_client:
        mine = await entitled_client.get("/me/settings")
        theirs = await other_client.get("/me/settings")

    assert mine.json()["ical_token"] != theirs.json()["ical_token"]


# --- the cadence ---------------------------------------------------------------------------


@pytest.mark.parametrize("cadence", ["daily", "weekly", "off"])
async def test_patch_sets_every_documented_cadence(entitled_client, cadence):
    r = await entitled_client.patch("/me/settings", json={"digest_cadence": cadence})
    assert r.status_code == 200
    assert r.json()["digest_cadence"] == cadence

    assert (await entitled_client.get("/me/settings")).json()["digest_cadence"] == cadence


async def test_patch_creates_the_row_when_nothing_read_it_first(entitled_client, session):
    r = await entitled_client.patch("/me/settings", json={"digest_cadence": "off"})
    assert r.status_code == 200
    assert r.json()["ical_token"]

    rows = (await session.execute(select(UserSettings))).scalars().all()
    assert [(row.user_id, row.digest_cadence) for row in rows] == [(entitled_client.user.id, "off")]


async def test_an_unknown_cadence_is_refused(entitled_client):
    r = await entitled_client.patch("/me/settings", json={"digest_cadence": "hourly"})
    assert r.status_code == 422


@pytest.mark.parametrize("payload", [{}, {"alert_stores": ["stream"]}])
async def test_a_patch_without_a_cadence_is_422(entitled_client, payload):
    """The cadence is the one setting there is, so it is required. An empty body is a client
    bug, and a body carrying only the retired `alert_stores` (ADR-0021) is a body without a
    cadence — refused the same way, with no special case for the old key."""
    r = await entitled_client.patch("/me/settings", json=payload)
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/me/push/vapid-public-key"), ("POST", "/me/push"), ("DELETE", "/me/push")],
)
async def test_the_push_routes_are_gone(entitled_client, method, path):
    """ADR-0021 retired Web Push, so its routes are absent from the router table — a 404, not
    the 503 `push_unavailable` an unconfigured deployment used to answer."""
    r = await entitled_client.request(method, path)
    assert r.status_code == 404


async def test_patching_the_cadence_leaves_the_token_alone(entitled_client):
    before = (await entitled_client.get("/me/settings")).json()
    after = (await entitled_client.patch("/me/settings", json={"digest_cadence": "daily"})).json()
    assert after["ical_token"] == before["ical_token"]


# --- the calendar token --------------------------------------------------------------------


async def test_rotating_issues_a_new_token_and_keeps_the_cadence(entitled_client, session):
    await entitled_client.patch("/me/settings", json={"digest_cadence": "daily"})
    before = (await entitled_client.get("/me/settings")).json()

    r = await entitled_client.post("/me/settings/ical-token/rotate")
    assert r.status_code == 200
    after = r.json()
    assert after["ical_token"] != before["ical_token"]
    assert after["digest_cadence"] == "daily"
    assert after["updated_at"] >= before["updated_at"]

    # The old value is gone rather than kept beside the new one: the point of rotation is that
    # a leaked URL stops resolving (D-34).
    rows = (await session.execute(select(UserSettings))).scalars().all()
    assert [row.ical_token for row in rows] == [after["ical_token"]]


async def test_rotating_creates_the_row_when_nothing_read_it_first(entitled_client):
    r = await entitled_client.post("/me/settings/ical-token/rotate")
    assert r.status_code == 200
    assert r.json()["digest_cadence"] == "weekly"


# --- entitlement lapsing (D-40) --------------------------------------------------------------


async def test_losing_entitlement_leaves_the_row_and_its_token_in_place(entitled_client, session):
    """Revocation suppresses and never destroys: a renewed grant resumes on the same calendar
    URL rather than breaking one the user already added to their phone."""
    from upmovies.app.services import entitlement_service

    issued = (await entitled_client.get("/me/settings")).json()["ical_token"]

    await entitlement_service.revoke(
        session, user_id=entitled_client.user.id, revoked_by=entitled_client.user
    )

    r = await entitled_client.get("/me/settings")
    assert r.status_code == 403

    row = (await session.execute(select(UserSettings))).scalars().one()
    assert row.ical_token == issued


# --- CSRF ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), [("PATCH", ""), ("POST", "/ical-token/rotate")])
async def test_writes_require_the_csrf_header(entitled_client, method, path):
    del entitled_client.headers["X-CSRF-Token"]
    r = await entitled_client.request(method, f"/me/settings{path}", json=_body(method))
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"


async def test_read_does_not_require_the_csrf_header(entitled_client):
    del entitled_client.headers["X-CSRF-Token"]
    r = await entitled_client.get("/me/settings")
    assert r.status_code == 200
