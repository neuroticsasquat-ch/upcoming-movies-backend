"""Response compression (NEU-1451): `GZipMiddleware` in `create_app`, tested once at the app
rather than per route. `GET /me/follows` is the reason it exists — unpaginated by decision, so
its payload is what a slow connection feels — and `GET /me` is the response it must not touch.

httpx decodes gzip transparently, so `content-encoding` is what says the wire was compressed;
the decoded body is the same either way."""

from upmovies.app.models import Follow
from upmovies.catalog.models import Person


async def _a_follows_list_over_one_kilobyte(session, user_id) -> None:
    for i in range(10):
        session.add(Person(id=2000 + i, name=f"Person {i}", profile_path=f"/p{i}.jpg"))
        session.add(
            Follow(user_id=user_id, entity_type="person", entity_id=str(2000 + i), source="manual")
        )
    await session.commit()


async def test_a_large_response_is_gzipped_for_a_client_that_accepts_it(entitled_client, session):
    await _a_follows_list_over_one_kilobyte(session, entitled_client.user.id)

    r = await entitled_client.get("/me/follows", headers={"Accept-Encoding": "gzip"})

    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert len(r.content) >= 1024
    assert len(r.json()["items"]) == 10


async def test_a_client_that_does_not_accept_gzip_gets_an_identity_body(entitled_client, session):
    await _a_follows_list_over_one_kilobyte(session, entitled_client.user.id)

    compressed = await entitled_client.get("/me/follows", headers={"Accept-Encoding": "gzip"})
    plain = await entitled_client.get("/me/follows", headers={"Accept-Encoding": "identity"})

    assert "content-encoding" not in plain.headers
    assert int(plain.headers["content-length"]) == len(plain.content)
    assert plain.content == compressed.content, "the shape is the same, only the wire differs"


async def test_me_is_never_compressed(authed_client):
    """`/me` carries the CSRF token (NEU-1382). It reflects no request input, so BREACH does
    not reach it anyway, but the 1 KB floor keeps it out of the compressor outright — this pins
    that the body stays under the floor, so a field added to `/me` that pushes it over is
    noticed here rather than argued about later."""
    r = await authed_client.get("/me", headers={"Accept-Encoding": "gzip"})

    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert len(r.content) < 1024
