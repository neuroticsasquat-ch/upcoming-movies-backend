from datetime import UTC, datetime

from upmovies.news.models import SourceDomain


async def _seed(session, domain, tier):
    now = datetime.now(UTC)
    session.add(
        SourceDomain(
            domain=domain,
            llm_tier=tier,
            llm_reason="r",
            admin_override="none",
            first_seen_at=now,
            judged_at=now,
            updated_at=now,
        )
    )
    await session.commit()


async def test_list_requires_auth(client):
    r = await client.get("/admin/sources")
    assert r.status_code == 401


async def test_list_forbidden_for_non_admin(authed_client):
    r = await authed_client.get("/admin/sources")
    assert r.status_code == 403


async def test_list_returns_domains(admin_authed_client, session):
    await _seed(session, "mshale.com", "low")
    r = await admin_authed_client.get("/admin/sources")
    assert r.status_code == 200
    body = r.json()
    assert any(d["domain"] == "mshale.com" and d["llm_tier"] == "low" for d in body)


async def test_set_override_roundtrip(admin_authed_client, session):
    await _seed(session, "mshale.com", "low")
    r = await admin_authed_client.post(
        "/admin/sources/mshale.com/override", json={"override": "block"}
    )
    assert r.status_code == 200
    assert r.json()["admin_override"] == "block"
    row = await session.get(SourceDomain, "mshale.com", populate_existing=True)
    assert row.admin_override == "block"


async def test_set_override_rejects_bad_value(admin_authed_client):
    r = await admin_authed_client.post("/admin/sources/x.com/override", json={"override": "banish"})
    assert r.status_code == 422


async def test_set_override_creates_unseen_domain(admin_authed_client, session):
    r = await admin_authed_client.post(
        "/admin/sources/newsite.test/override", json={"override": "trust"}
    )
    assert r.status_code == 200
    row = await session.get(SourceDomain, "newsite.test", populate_existing=True)
    assert row is not None and row.admin_override == "trust"
