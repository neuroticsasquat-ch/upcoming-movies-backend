"""`/me/follows` (D-10): the follow graph's CRUD, behind the entitlement gate (D-39)."""

import pytest

from tests.fixtures.catalog import add_film


@pytest.fixture
async def film(session):
    f = await add_film(session, tmdb_id=550)
    await session.commit()
    return f


# --- the gate ------------------------------------------------------------------------------


async def test_list_requires_auth(client):
    r = await client.get("/me/follows")
    assert r.status_code == 401


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_every_verb_is_403_for_an_unentitled_user(authed_client, method):
    # `authed_client`'s user has `entitled_until` NULL — the state every signup starts in
    # (D-37). Each verb is named so that a refactor that drops the gate from one of them fails
    # here rather than shipping.
    path = "/me/follows/person/1" if method == "DELETE" else "/me/follows"
    r = await authed_client.request(
        method, path, json={"entity_type": "person", "entity_id": "1"} if method == "POST" else None
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"


# --- following -----------------------------------------------------------------------------


async def test_follow_a_person_and_list_it(entitled_client, session):
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt"))
    await session.commit()

    r = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "287"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["entity_type"] == "person"
    assert body["entity_id"] == "287"
    assert body["source"] == "manual"

    r = await entitled_client.get("/me/follows")
    assert r.status_code == 200
    assert [(f["entity_type"], f["entity_id"]) for f in r.json()["items"]] == [("person", "287")]


async def test_following_twice_returns_the_existing_row(entitled_client, session):
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt"))
    await session.commit()

    first = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "287"}
    )
    again = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "287"}
    )
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()

    r = await entitled_client.get("/me/follows")
    assert len(r.json()["items"]) == 1


async def test_entity_id_is_normalised_so_one_entity_cannot_be_followed_twice(
    entitled_client, session
):
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt"))
    await session.commit()

    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "287"})
    r = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "0287"}
    )
    assert r.status_code == 200
    assert r.json()["entity_id"] == "287"


async def test_following_an_entity_the_catalog_does_not_hold_is_404(entitled_client):
    r = await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "1"})
    assert r.status_code == 404
    assert r.json()["detail"] == "entity_not_found"

    r = await entitled_client.get("/me/follows")
    assert r.json()["items"] == []


async def test_every_entity_type_resolves_against_its_catalog_table(entitled_client, session, film):
    from upmovies.catalog.models import Collection, Person, ProductionCompany

    session.add(Person(id=287, name="Brad Pitt"))
    session.add(ProductionCompany(id=508, name="Regency"))
    session.add(Collection(id=10, name="Fight Club Collection"))
    await session.commit()

    for entity_type, entity_id in [
        ("person", "287"),
        ("company", "508"),
        ("franchise", "10"),
        ("title", str(film.id)),
    ]:
        r = await entitled_client.post(
            "/me/follows", json={"entity_type": entity_type, "entity_id": entity_id}
        )
        assert r.status_code == 201, (entity_type, r.json())
        assert r.json()["entity_id"] == entity_id

    r = await entitled_client.get("/me/follows")
    assert {(f["entity_type"], f["entity_id"]) for f in r.json()["items"]} == {
        ("person", "287"),
        ("company", "508"),
        ("franchise", "10"),
        ("title", str(film.id)),
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"entity_type": "person", "entity_id": "abc"},
        {"entity_type": "person", "entity_id": "0"},
        {"entity_type": "person", "entity_id": "-4"},
        {"entity_type": "title", "entity_id": "287"},
        {"entity_type": "actor", "entity_id": "287"},
        {"entity_type": "person"},
    ],
)
async def test_an_id_of_the_wrong_shape_for_its_type_is_422(entitled_client, payload):
    r = await entitled_client.post("/me/follows", json=payload)
    assert r.status_code == 422


async def test_a_user_sees_only_their_own_follows(entitled_client, make_user, session):
    from upmovies.app.models import Follow
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt"))
    other = await make_user(email="other@example.com")
    session.add(Follow(user_id=other.id, entity_type="person", entity_id="287", source="manual"))
    await session.commit()

    r = await entitled_client.get("/me/follows")
    assert r.json()["items"] == []


# --- unfollowing ---------------------------------------------------------------------------


async def test_unfollow_removes_the_row(entitled_client, session):
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt"))
    await session.commit()
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "287"})

    r = await entitled_client.delete("/me/follows/person/287")
    assert r.status_code == 204

    r = await entitled_client.get("/me/follows")
    assert r.json()["items"] == []


async def test_unfollow_of_something_not_followed_is_404(entitled_client):
    r = await entitled_client.delete("/me/follows/person/287")
    assert r.status_code == 404
    assert r.json()["detail"] == "follow_not_found"


async def test_unfollow_normalises_the_id_like_follow_does(entitled_client, session):
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt"))
    await session.commit()
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "287"})

    r = await entitled_client.delete("/me/follows/person/0287")
    assert r.status_code == 204


async def test_unfollow_with_a_malformed_id_is_422(entitled_client):
    r = await entitled_client.delete("/me/follows/title/not-a-uuid")
    assert r.status_code == 422
    r = await entitled_client.delete("/me/follows/actor/1")
    assert r.status_code == 422


async def test_unfollow_leaves_another_users_follow_alone(entitled_client, make_user, session):
    from sqlalchemy import select

    from upmovies.app.models import Follow
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt"))
    other = await make_user(email="other@example.com")
    session.add(Follow(user_id=other.id, entity_type="person", entity_id="287", source="manual"))
    await session.commit()

    r = await entitled_client.delete("/me/follows/person/287")
    assert r.status_code == 404
    remaining = (await session.execute(select(Follow))).scalars().all()
    assert [f.user_id for f in remaining] == [other.id]


# --- CSRF ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_writes_require_the_csrf_header(entitled_client, method):
    del entitled_client.headers["X-CSRF-Token"]
    path = "/me/follows/person/287" if method == "DELETE" else "/me/follows"
    r = await entitled_client.request(
        method,
        path,
        json={"entity_type": "person", "entity_id": "287"} if method == "POST" else None,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"


async def test_list_does_not_require_csrf_header(entitled_client):
    del entitled_client.headers["X-CSRF-Token"]
    r = await entitled_client.get("/me/follows")
    assert r.status_code == 200


# --- derived watchlist items (D-13) --------------------------------------------------------


async def test_following_a_director_derives_their_in_play_films(entitled_client, session):
    """The synchronous half of the derivation (D-13): the items are on the list by the time the
    POST answers, so the client that just followed can render the watchlist without a second
    round trip waiting on the sweep."""
    from datetime import date

    from upmovies.catalog.models import FilmCredit, Person

    film = await add_film(session, tmdb_id=551, release_date=date(2099, 1, 1))
    session.add(Person(id=525, name="Christopher Nolan"))
    await session.flush()
    session.add(
        FilmCredit(
            credit_id="c-551-525",
            film_id=film.id,
            person_id=525,
            credit_type="crew",
            job="Director",
            department="Directing",
        )
    )
    await session.commit()

    r = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "525"}
    )
    assert r.status_code == 201

    r = await entitled_client.get("/me/watchlist")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["film"]["title"], i["source"], i["alert_prefs"]) for i in items] == [
        (film.title, "derived_from_follow", ["stream"])
    ]


async def test_a_dismissed_film_is_never_derived_again(entitled_client, session):
    """Follow → derive → remove (which writes the dismissal) → follow something else that
    reaches the same film. The film stays off the list, which is the whole point of the
    dismissal row being permanent."""
    from datetime import date

    from upmovies.catalog.models import Collection

    session.add(Collection(id=10, name="A Franchise"))
    film = await add_film(session, tmdb_id=552, release_date=date(2099, 1, 1), collection_id=10)
    await session.commit()

    r = await entitled_client.post(
        "/me/follows", json={"entity_type": "title", "entity_id": str(film.id)}
    )
    assert r.status_code == 201
    assert len((await entitled_client.get("/me/watchlist")).json()["items"]) == 1

    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 204

    r = await entitled_client.post(
        "/me/follows", json={"entity_type": "franchise", "entity_id": "10"}
    )
    assert r.status_code == 201
    assert (await entitled_client.get("/me/watchlist")).json()["items"] == []


async def test_following_a_person_with_no_qualifying_credit_derives_nothing(
    entitled_client, session
):
    """A follow is not a watchlist add: the writer cut (D-13) means the POST can legitimately
    leave the watchlist empty, and the route must still answer 201."""
    from datetime import date

    from upmovies.catalog.models import FilmCredit, Person

    film = await add_film(session, tmdb_id=553, release_date=date(2099, 1, 1))
    session.add(Person(id=488, name="A Writer"))
    await session.flush()
    session.add(
        FilmCredit(
            credit_id="c-553-488",
            film_id=film.id,
            person_id=488,
            credit_type="crew",
            job="Screenplay",
            department="Writing",
        )
    )
    await session.commit()

    r = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "488"}
    )
    assert r.status_code == 201
    assert (await entitled_client.get("/me/watchlist")).json()["items"] == []


# --- entity names on the list (NEU-1396) ---------------------------------------------------


async def test_every_entity_type_carries_its_name_and_image(entitled_client, session, film):
    """One name column per type (D-10's four entities), each from its own catalog table."""
    from upmovies.catalog.models import Collection, Person, ProductionCompany

    session.add(Person(id=287, name="Brad Pitt", profile_path="/pitt.jpg"))
    session.add(ProductionCompany(id=508, name="Regency", logo_path="/regency.png"))
    session.add(Collection(id=10, name="Fight Club Collection", poster_path="/fc.jpg"))
    film.poster_path = "/film.jpg"
    await session.commit()

    for entity_type, entity_id in [
        ("person", "287"),
        ("company", "508"),
        ("franchise", "10"),
        ("title", str(film.id)),
    ]:
        r = await entitled_client.post(
            "/me/follows", json={"entity_type": entity_type, "entity_id": entity_id}
        )
        assert r.status_code == 201, (entity_type, r.json())

    r = await entitled_client.get("/me/follows")
    assert {(f["entity_type"], f["name"], f["image_path"]) for f in r.json()["items"]} == {
        ("person", "Brad Pitt", "/pitt.jpg"),
        ("company", "Regency", "/regency.png"),
        ("franchise", "Fight Club Collection", "/fc.jpg"),
        ("title", film.title, "/film.jpg"),
    }


async def test_a_follow_the_catalog_cannot_resolve_is_listed_with_nulls(entitled_client, session):
    """The row survives with `name: null` rather than being filtered out or 500ing.

    A follow can outlive the entity it names — a person purged from TMDB, a row written before a
    backfill — and D-40 says nothing here deletes user graph rows. Dropping the unjoinable rows
    is the obvious wrong implementation: it would hide a follow the user can still see the
    effects of, and it would quietly break the restore-after-revocation guarantee."""
    from sqlalchemy import select

    from upmovies.app.models import Follow
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt", profile_path="/pitt.jpg"))
    await session.commit()

    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "287"})

    # A follow of an entity the catalog has never held, written straight to the table: the route
    # refuses to create one (404), which is exactly why it has to be forged here.
    followed = (await session.execute(select(Follow))).scalars().first()
    assert followed is not None
    session.add(
        Follow(
            user_id=followed.user_id,
            entity_type="person",
            entity_id="999999",
            source="letterboxd_import",
        )
    )
    await session.commit()

    r = await entitled_client.get("/me/follows")
    assert r.status_code == 200
    items = {f["entity_id"]: f for f in r.json()["items"]}
    assert set(items) == {"287", "999999"}
    assert (items["999999"]["name"], items["999999"]["image_path"]) == (None, None)
    assert items["287"]["name"] == "Brad Pitt"


async def test_a_malformed_entity_id_does_not_break_the_list(entitled_client, session):
    """Belt and braces, like the shape guards in `app/follow_queries.py`: `normalise_entity_id`
    keeps these out of the table, but the importers (D-15, D-16) call the service directly, and
    one bad row must degrade to a null name rather than abort the whole statement."""
    from sqlalchemy import select

    from upmovies.app.models import Follow
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt"))
    await session.commit()
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "287"})

    followed = (await session.execute(select(Follow))).scalars().first()
    assert followed is not None
    session.add(
        Follow(
            user_id=followed.user_id,
            entity_type="person",
            entity_id="not-a-number",
            source="letterboxd_import",
        )
    )
    session.add(
        Follow(
            user_id=followed.user_id,
            entity_type="title",
            entity_id="not-a-uuid",
            source="letterboxd_import",
        )
    )
    await session.commit()

    r = await entitled_client.get("/me/follows")
    assert r.status_code == 200
    items = {f["entity_id"]: f for f in r.json()["items"]}
    assert items["not-a-number"]["name"] is None
    assert items["not-a-uuid"]["name"] is None
    assert items["287"]["name"] == "Brad Pitt"


async def test_creating_a_follow_answers_with_the_name(entitled_client, session):
    """`FollowOut` is the POST's response too, so a follow button can label its new row without
    a second request — and the idempotent second POST answers identically."""
    from upmovies.catalog.models import Person

    session.add(Person(id=287, name="Brad Pitt", profile_path="/pitt.jpg"))
    await session.commit()

    first = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "287"}
    )
    assert first.status_code == 201
    assert (first.json()["name"], first.json()["image_path"]) == ("Brad Pitt", "/pitt.jpg")

    again = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "287"}
    )
    assert again.status_code == 200
    assert again.json() == first.json()
