"""The grant surface (D-38) and the request-time gate it feeds (D-39)."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from httpx import Request as HRequest
from sqlalchemy import select

from upmovies.app import tokens
from upmovies.app.entitlements import entitled_user_clause, require_entitled
from upmovies.app.models import EmailToken, Session, User
from upmovies.app.repos import session_repo

_AUDIT_LOGGER = "upmovies.app.services.entitlement_service"

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
PAST = datetime(2000, 1, 1, tzinfo=UTC)


def _entitlement_url(user: User) -> str:
    return f"/admin/users/{user.id}/entitlement"


# --- the list ------------------------------------------------------------------------------


async def test_list_requires_auth(client):
    r = await client.get("/admin/users")
    assert r.status_code == 401


async def test_list_forbidden_for_non_admin(authed_client):
    r = await authed_client.get("/admin/users")
    assert r.status_code == 403


async def test_list_returns_the_account_fields_the_grant_page_renders(
    admin_authed_client, make_user
):
    user = await make_user(email="grantee@example.com")
    r = await admin_authed_client.get("/admin/users")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2  # the admin the fixture signed in as, plus this one
    row = next(u for u in body["items"] if u["email"] == "grantee@example.com")
    assert row == {
        "id": str(user.id),
        "email": "grantee@example.com",
        "is_admin": False,
        "email_verified_at": None,
        "entitled_until": None,
        "created_at": row["created_at"],
    }


async def test_list_searches_by_email_case_insensitively(admin_authed_client, make_user):
    await make_user(email="Ada@example.com")
    await make_user(email="grace@example.com")
    r = await admin_authed_client.get("/admin/users", params={"q": "ADA"})
    assert r.status_code == 200
    body = r.json()
    assert [u["email"] for u in body["items"]] == ["Ada@example.com"]
    # The total is the total *for the search*, not for the table — otherwise the page cannot
    # tell whether a narrowed result has a second page.
    assert body["total"] == 1


async def test_list_pages_and_reports_the_total_it_paged(admin_authed_client, make_user):
    for i in range(3):
        await make_user(email=f"u{i}@example.com")
    r = await admin_authed_client.get("/admin/users", params={"limit": 2, "offset": 0})
    first = r.json()
    assert len(first["items"]) == 2
    assert (first["total"], first["limit"], first["offset"]) == (4, 2, 0)

    r = await admin_authed_client.get("/admin/users", params={"limit": 2, "offset": 2})
    second = r.json()
    assert len(second["items"]) == 2
    ids = {u["id"] for u in first["items"]} | {u["id"] for u in second["items"]}
    assert len(ids) == 4


async def test_search_treats_like_wildcards_as_literal_characters(admin_authed_client, make_user):
    # The value comes from a search box, so `%` is a character in an address someone is looking
    # for, not a wildcard they meant to write.
    await make_user(email="a%b@example.com")
    await make_user(email="unrelated@example.com")
    r = await admin_authed_client.get("/admin/users", params={"q": "a%b"})
    assert [u["email"] for u in r.json()["items"]] == ["a%b@example.com"]
    r = await admin_authed_client.get("/admin/users", params={"q": "%"})
    assert [u["email"] for u in r.json()["items"]] == ["a%b@example.com"]


async def test_list_does_not_require_csrf_header(admin_authed_client):
    # The frontend omits X-CSRF-Token on GETs (safe methods); the list must still work.
    del admin_authed_client.headers["X-CSRF-Token"]
    r = await admin_authed_client.get("/admin/users")
    assert r.status_code == 200


# --- granting and revoking -----------------------------------------------------------------


async def test_grant_requires_auth(client, make_user):
    user = await make_user()
    r = await client.put(_entitlement_url(user), json={"entitled_until": FUTURE.isoformat()})
    assert r.status_code == 401


async def test_grant_forbidden_for_non_admin(authed_client, make_user):
    user = await make_user(email="other@example.com")
    r = await authed_client.put(_entitlement_url(user), json={"entitled_until": FUTURE.isoformat()})
    assert r.status_code == 403


async def test_revoke_forbidden_for_non_admin(authed_client, make_user):
    user = await make_user(email="other@example.com")
    r = await authed_client.delete(_entitlement_url(user))
    assert r.status_code == 403


async def test_grant_requires_csrf(admin_authed_client, make_user):
    user = await make_user()
    del admin_authed_client.headers["X-CSRF-Token"]
    r = await admin_authed_client.put(
        _entitlement_url(user), json={"entitled_until": FUTURE.isoformat()}
    )
    assert r.status_code == 403


async def test_revoke_requires_csrf(admin_authed_client, make_user):
    user = await make_user()
    del admin_authed_client.headers["X-CSRF-Token"]
    r = await admin_authed_client.delete(_entitlement_url(user))
    assert r.status_code == 403


async def test_grant_then_extend_then_revoke(admin_authed_client, session, make_user):
    user = await make_user()

    r = await admin_authed_client.put(
        _entitlement_url(user), json={"entitled_until": FUTURE.isoformat()}
    )
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["entitled_until"]) == FUTURE

    later = FUTURE + timedelta(days=365)
    r = await admin_authed_client.put(
        _entitlement_url(user), json={"entitled_until": later.isoformat()}
    )
    assert datetime.fromisoformat(r.json()["entitled_until"]) == later

    r = await admin_authed_client.delete(_entitlement_url(user))
    assert r.status_code == 200
    assert r.json()["entitled_until"] is None

    await session.refresh(user)
    assert user.entitled_until is None


async def test_a_naive_expiry_is_read_as_utc(admin_authed_client, make_user):
    # The admin page posts a date picker's value, which need not carry an offset.
    user = await make_user()
    r = await admin_authed_client.put(
        _entitlement_url(user), json={"entitled_until": "2099-01-01T00:00:00"}
    )
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["entitled_until"]) == FUTURE


async def test_a_past_expiry_is_an_accepted_way_to_end_a_grant(admin_authed_client, make_user):
    user = await make_user()
    r = await admin_authed_client.put(
        _entitlement_url(user), json={"entitled_until": PAST.isoformat()}
    )
    assert r.status_code == 200
    # Still listed with its lapsed date, so "expired" and "never granted" stay distinguishable.
    assert datetime.fromisoformat(r.json()["entitled_until"]) == PAST


async def test_grant_and_revoke_404_an_unknown_user(admin_authed_client):
    missing = "00000000-0000-0000-0000-000000000000"
    r = await admin_authed_client.put(
        f"/admin/users/{missing}/entitlement", json={"entitled_until": FUTURE.isoformat()}
    )
    assert r.status_code == 404
    r = await admin_authed_client.delete(f"/admin/users/{missing}/entitlement")
    assert r.status_code == 404


async def test_revoking_suppresses_and_never_destroys(admin_authed_client, session, make_user):
    """D-40: losing entitlement leaves the account's rows exactly as they were.

    The follow, watchlist and dismissal tables arrive in M3, so what is asserted here is every
    per-user row that exists today — the session that keeps them signed in and a mailed token —
    plus the account's own columns. The rule this pins down is that revoking writes one column
    and nothing else."""
    user = await make_user(email="lapsing@example.com", display_name="Lapsing")
    verified_at = datetime.now(UTC) - timedelta(days=5)
    user.email_verified_at = verified_at
    session.add(
        EmailToken(
            token=tokens.new_email_token(),
            user_id=user.id,
            purpose="verify",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await session_repo.create(
        session,
        session_id=tokens.new_session_id(),
        user_id=user.id,
        ttl_days=30,
        user_agent=None,
        ip=None,
    )
    await session.commit()

    await admin_authed_client.put(
        _entitlement_url(user), json={"entitled_until": FUTURE.isoformat()}
    )
    await admin_authed_client.delete(_entitlement_url(user))

    await session.refresh(user)
    assert user.entitled_until is None
    assert user.email == "lapsing@example.com"
    assert user.display_name == "Lapsing"
    assert user.email_verified_at == verified_at
    assert user.is_admin is False
    sessions = (
        (await session.execute(select(Session).where(Session.user_id == user.id))).scalars().all()
    )
    assert len(sessions) == 1
    email_tokens = (
        (await session.execute(select(EmailToken).where(EmailToken.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(email_tokens) == 1


async def test_both_writes_log_the_acting_admin_and_the_target(
    admin_authed_client, make_user, caplog
):
    """The audit line is the whole reason this surface is session-authed rather than
    ADMIN_TOKEN'd: a grant is one person's decision about another, and a log that named
    neither would not be worth writing."""
    user = await make_user(email="audited@example.com")
    admin = admin_authed_client.user

    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        await admin_authed_client.put(
            _entitlement_url(user), json={"entitled_until": FUTURE.isoformat()}
        )
        await admin_authed_client.delete(_entitlement_url(user))

    audit = [r for r in caplog.records if r.name == _AUDIT_LOGGER]
    assert [r.levelno for r in audit] == [logging.INFO, logging.INFO]
    granted, revoked = (r.getMessage() for r in audit)
    assert f"admin_id={admin.id}" in granted and f"user_id={user.id}" in granted
    assert FUTURE.isoformat() in granted
    assert f"admin_id={admin.id}" in revoked and f"user_id={user.id}" in revoked
    # The previous value is carried so a revoke says what was taken away, not just that
    # something was.
    assert FUTURE.isoformat() in revoked


# --- what the gate does with the column ----------------------------------------------------


async def test_me_reports_entitlement_so_the_frontend_can_branch(
    admin_authed_client, authed_client
):
    # D-41: AuthContext reads a boolean, sourced from the `me` payload.
    assert (await authed_client.get("/me")).json()["entitled"] is False
    await admin_authed_client.put(
        _entitlement_url(authed_client.user), json={"entitled_until": FUTURE.isoformat()}
    )
    assert (await authed_client.get("/me")).json()["entitled"] is True


@pytest.fixture
def gated_app():
    """A throwaway app with one route behind `require_entitled()`.

    The real `/me/*` routes do not apply the gate yet — that belongs to the M3 and M7 tickets
    that create them — so the dependency is exercised against a route of the test's own."""
    app = FastAPI()

    @app.get("/gated")
    async def gated(user: User = Depends(require_entitled())) -> dict[str, str]:
        return {"email": user.email}

    return app


async def _gated_client(gated_app, session, user: User) -> AsyncClient:
    sess_id = tokens.new_session_id()
    await session_repo.create(
        session, session_id=sess_id, user_id=user.id, ttl_days=30, user_agent=None, ip=None
    )
    await session.commit()

    async def _inject(request: HRequest) -> None:
        request.headers["cookie"] = f"upmovies_session={sess_id}"

    return AsyncClient(
        transport=ASGITransport(app=gated_app),
        base_url="https://test",
        event_hooks={"request": [_inject]},
    )


async def test_a_gated_route_401s_an_anonymous_caller(gated_app):
    async with AsyncClient(transport=ASGITransport(app=gated_app), base_url="https://test") as c:
        assert (await c.get("/gated")).status_code == 401


async def test_a_gated_route_403s_an_unentitled_user(gated_app, session, make_user):
    user = await make_user()
    async with await _gated_client(gated_app, session, user) as c:
        r = await c.get("/gated")
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"


async def test_a_gated_route_403s_a_lapsed_grant(gated_app, session, make_user):
    user = await make_user()
    user.entitled_until = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    async with await _gated_client(gated_app, session, user) as c:
        r = await c.get("/gated")
    assert r.status_code == 403


async def test_a_gated_route_admits_an_entitled_user(gated_app, session, make_user):
    user = await make_user()
    user.entitled_until = FUTURE
    await session.commit()
    async with await _gated_client(gated_app, session, user) as c:
        r = await c.get("/gated")
    assert r.status_code == 200
    assert r.json() == {"email": user.email}


async def test_entitled_user_clause_selects_only_live_grants(session, make_user):
    """D-39's batch half: the predicate the notify, digest and sweep passes filter on."""
    await make_user(email="never@example.com")
    lapsed = await make_user(email="lapsed@example.com")
    lapsed.entitled_until = datetime.now(UTC) - timedelta(seconds=1)
    live = await make_user(email="live@example.com")
    live.entitled_until = FUTURE
    await session.commit()

    rows = (await session.execute(select(User.email).where(entitled_user_clause()))).scalars().all()
    assert rows == ["live@example.com"]
