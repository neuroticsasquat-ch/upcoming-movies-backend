"""The admin invite surface: session-authed (NEU-1408, D-1408.1), minting and listing codes."""

from datetime import UTC, datetime

from upmovies.app.repos import invite_repo
from upmovies.app.services import invite_service
from upmovies.config import get_settings

# --- auth matrix (mirrors test_users_admin.py) ---------------------------------------------


async def test_list_requires_auth(client):
    r = await client.get("/admin/invites")
    assert r.status_code == 401
    assert r.json()["detail"] == "auth_required"


async def test_list_forbidden_for_non_admin(authed_client):
    r = await authed_client.get("/admin/invites")
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


async def test_list_does_not_require_csrf_header(admin_authed_client):
    # The frontend omits X-CSRF-Token on GETs (safe methods); the list must still work.
    del admin_authed_client.headers["X-CSRF-Token"]
    r = await admin_authed_client.get("/admin/invites")
    assert r.status_code == 200


async def test_create_requires_auth(client):
    r = await client.post("/admin/invites", json={})
    assert r.status_code == 401
    assert r.json()["detail"] == "auth_required"


async def test_create_forbidden_for_non_admin(authed_client):
    r = await authed_client.post("/admin/invites", json={})
    assert r.status_code == 403


async def test_create_requires_csrf_header(admin_authed_client):
    del admin_authed_client.headers["X-CSRF-Token"]
    r = await admin_authed_client.post("/admin/invites", json={})
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"


async def test_bearer_token_no_longer_authenticates(client):
    # D-1408.1: the ADMIN_TOKEN path is removed, not kept alongside the session gate.
    r = await client.get(
        "/admin/invites",
        headers={"Authorization": f"Bearer {get_settings().admin_token}"},
    )
    assert r.status_code == 401


# --- minting --------------------------------------------------------------------------------


async def test_create_invite_returns_an_unconsumed_code(admin_authed_client):
    r = await admin_authed_client.post("/admin/invites", json={})
    assert r.status_code == 201
    body = r.json()
    assert body["code"]
    assert body["email_hint"] is None
    assert body["consumed_at"] is None
    assert body["consumed_by_user_id"] is None
    assert body["consumed_by_email"] is None


async def test_create_invite_with_email_hint(admin_authed_client):
    r = await admin_authed_client.post("/admin/invites", json={"email_hint": "alice@example.com"})
    assert r.status_code == 201
    assert r.json()["email_hint"] == "alice@example.com"


# --- listing --------------------------------------------------------------------------------


async def test_list_invites_returns_issued_codes_newest_first(admin_authed_client, make_invite):
    first = await make_invite()
    second = await make_invite(email_hint="bob@example.com")
    r = await admin_authed_client.get("/admin/invites")
    assert r.status_code == 200
    body = r.json()
    assert [i["code"] for i in body] == [second, first]
    assert body[0]["email_hint"] == "bob@example.com"
    assert body[0]["consumed_by_email"] is None


async def test_consumed_invite_lists_the_consumers_email(
    admin_authed_client, session, make_invite, make_user
):
    # D-1408.2: the page shows who spent a code by address, not by id.
    code = await make_invite(email_hint="carol@example.com")
    user = await make_user(email="carol@example.com")
    invite = await invite_repo.get(session, code)
    assert invite is not None
    await invite_service.consume(session, invite=invite, user_id=user.id, now=datetime.now(UTC))
    await session.commit()

    r = await admin_authed_client.get("/admin/invites")
    assert r.status_code == 200
    row = next(i for i in r.json() if i["code"] == code)
    assert row["consumed_at"] is not None
    assert row["consumed_by_user_id"] == str(user.id)
    assert row["consumed_by_email"] == "carol@example.com"
