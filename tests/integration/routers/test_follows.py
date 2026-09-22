"""`/me/follows` (D-10): the follow graph's CRUD, behind the entitlement gate (D-39)."""

from uuid import uuid4

import pytest

from tests.fixtures.catalog import add_film


def film_uuid() -> str:
    """A well-formed film id that names nothing: it passes `normalise_entity_id` and then
    finds no row, which is how the routes' 404 is reached rather than their 422."""
    return str(uuid4())


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


# --- headline_release on title rows (EF-14, EF-15) ------------------------------------------


async def test_a_title_row_carries_the_films_headline_release(entitled_client, session):
    """EF-14: the follows page shows a followed film's date, from the one batch query every
    film row on this site reads (`catalog.headline_release`). The row that used to carry it was
    the watchlist's; this is where it lives now."""
    from datetime import UTC, date, datetime

    from upmovies.catalog.models import FilmReleaseDate

    film = await add_film(session, tmdb_id=560, release_date=date(2099, 1, 1))
    session.add(
        FilmReleaseDate(
            film_id=film.id,
            iso_3166_1="US",
            release_type=3,
            release_date=datetime(2099, 3, 4, tzinfo=UTC),
        )
    )
    await session.commit()

    await entitled_client.post(
        "/me/follows", json={"entity_type": "title", "entity_id": str(film.id)}
    )

    rows = (await entitled_client.get("/me/follows")).json()["items"]
    assert [r["headline_release"] for r in rows] == [
        {"date": "2099-03-04", "kind": "upcoming", "country": "US", "bucket": "wide"}
    ]


async def test_the_follow_button_answers_with_the_same_date_the_list_does(entitled_client, session):
    """One row, one truth. The film page follows through this route and reconciles its cache
    from the response (NEU-1405), so a write that answered `null` while `GET /me/follows`
    answered with a real date would render "No date yet" on the film the user just followed."""
    from datetime import UTC, date, datetime

    from upmovies.catalog.models import FilmReleaseDate

    film = await add_film(session, tmdb_id=562, release_date=date(2099, 1, 1))
    session.add(
        FilmReleaseDate(
            film_id=film.id,
            iso_3166_1="US",
            release_type=3,
            release_date=datetime(2099, 3, 4, tzinfo=UTC),
        )
    )
    await session.commit()

    written = await entitled_client.post(
        "/me/follows", json={"entity_type": "title", "entity_id": str(film.id)}
    )
    assert written.status_code == 201

    listed = (await entitled_client.get("/me/follows")).json()["items"]
    assert written.json()["headline_release"] == listed[0]["headline_release"]
    assert written.json()["headline_release"]["date"] == "2099-03-04"


async def test_a_title_row_with_no_displayable_date_carries_null(entitled_client, session):
    """The absence is a null, not a missing key: a film with no displayable release row and no
    primary date has no headline release, and the page renders "No date yet"."""
    film = await add_film(session, tmdb_id=561, release_date=None)
    await session.commit()

    await entitled_client.post(
        "/me/follows", json={"entity_type": "title", "entity_id": str(film.id)}
    )

    rows = (await entitled_client.get("/me/follows")).json()["items"]
    assert [r["headline_release"] for r in rows] == [None]


@pytest.mark.parametrize(
    ("entity_type", "entity_id"),
    [("person", "525"), ("company", "711"), ("franchise", "10")],
)
async def test_an_entity_row_carries_no_headline_release(
    entitled_client, session, entity_type, entity_id
):
    """EF-14: only a film has a date worth leading with. Answering a person row with, say, the
    next release they are credited on would be the indirect reach this project has just taken
    away, smuggled back in as a column."""
    from upmovies.catalog.models import Collection, Person, ProductionCompany

    session.add_all(
        [
            Person(id=525, name="Christopher Nolan"),
            ProductionCompany(id=711, name="A Studio"),
            Collection(id=10, name="A Franchise"),
        ]
    )
    await session.commit()

    await entitled_client.post(
        "/me/follows", json={"entity_type": entity_type, "entity_id": entity_id}
    )

    rows = (await entitled_client.get("/me/follows")).json()["items"]
    assert [r["headline_release"] for r in rows] == [None]


async def test_following_a_director_puts_no_film_on_the_list(entitled_client, session):
    """EF-14 and EF-3, the cutover, from the route the frontend actually reads. A person follow
    is one row naming a person — it does not put that person's films anywhere, and there is no
    longer an endpoint that would list them."""
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

    rows = (await entitled_client.get("/me/follows")).json()["items"]
    assert [(r["entity_type"], r["entity_id"]) for r in rows] == [("person", "525")]


@pytest.mark.parametrize("path", ["/me/watchlist", "/me/watchlist/"])
async def test_the_watchlist_routes_are_gone(entitled_client, path):
    """EF-14: `GET`, `POST` and `DELETE /me/watchlist` are removed outright, not left answering
    an empty list — a client still calling them should find out, and the M3 frontend ticket is
    what stops calling them."""
    assert (await entitled_client.get(path)).status_code == 404
    assert (await entitled_client.post(path, json={"film_id": str(uuid4())})).status_code == 404


async def test_the_watchlist_delete_route_is_gone(entitled_client):
    assert (await entitled_client.delete(f"/me/watchlist/{uuid4()}")).status_code == 404


@pytest.mark.parametrize("entity_type", ["person", "company", "franchise", "title"])
async def test_a_coverage_in_the_body_is_ignored_not_refused(entitled_client, session, entity_type):
    """The M2→M3 contract (EF-1, spec §6). The backend drops the tier a deploy before the
    frontend stops sending it, so `coverage` has to be dropped silently: 422-ing it — which is
    what D-1414.6 did for the three non-person types, and what the DTO did for a bad value —
    would break every follow button in the live client for the length of the gap.

    All four types, because the old rule was *type-dependent* and this one is not: `person`
    accepted the field and the other three refused it, so a check surviving on either side
    would show up here."""
    from upmovies.catalog.models import Collection, Person, ProductionCompany

    film = await add_film(session, tmdb_id=560)
    session.add_all([Person(id=620, name="A Person"), ProductionCompany(id=620, name="A Studio")])
    session.add(Collection(id=620, name="A Franchise"))
    await session.commit()
    entity_id = {
        "person": "620",
        "company": "620",
        "franchise": "620",
        "title": str(film.id),
    }[entity_type]

    r = await entitled_client.post(
        "/me/follows",
        json={"entity_type": entity_type, "entity_id": entity_id, "coverage": "major"},
    )
    assert r.status_code == 201
    assert "coverage" not in r.json()

    # And a value the old CHECK never admitted is ignored on the same terms — the field is not
    # declared, so there is nothing left to validate it against.
    r = await entitled_client.post(
        "/me/follows",
        json={"entity_type": entity_type, "entity_id": entity_id, "coverage": "everything"},
    )
    assert r.status_code == 200


async def test_patching_a_follow_is_a_200_no_op(entitled_client, session):
    """A binary follow has nothing to PATCH (EF-1). The route outlives its purpose only
    because the live frontend still calls it across the M2→M3 gap, so it answers 200 with the
    row unchanged rather than 404-ing or 405-ing a control the user is about to lose.

    Any body at all, including none: the request model is gone with the field it carried."""
    from upmovies.catalog.models import Person

    session.add(Person(id=630, name="A Writer"))
    await session.commit()
    created = await entitled_client.post(
        "/me/follows", json={"entity_type": "person", "entity_id": "630"}
    )
    assert created.status_code == 201

    for body in ({"coverage": "major"}, {"coverage": None}, {}):
        r = await entitled_client.patch("/me/follows/person/630", json=body)
        assert r.status_code == 200
        assert r.json() == created.json()


@pytest.mark.parametrize("entity_type", ["company", "franchise", "title"])
async def test_patching_a_non_person_follow_is_no_longer_422(entitled_client, session, entity_type):
    """`coverage_not_applicable` (D-1414.6) is gone with the tier it guarded. These three used
    to be refused outright; now they 404 like any other follow that does not exist, which is
    the only thing left for the route to be wrong about."""
    entity_id = {"company": "1", "franchise": "1", "title": str(film_uuid())}[entity_type]
    r = await entitled_client.patch(f"/me/follows/{entity_type}/{entity_id}", json={})
    assert r.status_code == 404
    assert r.json()["detail"] == "follow_not_found"


async def test_patching_a_follow_that_does_not_exist_is_404(entitled_client):
    r = await entitled_client.patch("/me/follows/person/999", json={})
    assert r.status_code == 404
    assert r.json()["detail"] == "follow_not_found"


async def test_patching_a_malformed_entity_id_is_422(entitled_client):
    """The one refusal the route keeps: the id is a path parameter, not a body field, and a
    non-numeric person id is a request that cannot name a row."""
    r = await entitled_client.patch("/me/follows/person/nm0000233", json={})
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid_entity_id"


async def test_patching_a_follow_requires_the_csrf_header(entitled_client, session):
    """A no-op behind CSRF on purpose: the guard belongs to the *method*, and a route that
    dropped it while it happened to write nothing would be a hole the day it writes again."""
    from upmovies.catalog.models import Person

    session.add(Person(id=491, name="A Fourth Writer"))
    await session.commit()
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "491"})

    del entitled_client.headers["X-CSRF-Token"]
    r = await entitled_client.patch("/me/follows/person/491", json={})
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"


async def test_patching_a_follow_is_403_for_an_unentitled_user(authed_client):
    r = await authed_client.patch("/me/follows/person/1", json={})
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"


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


async def test_an_out_of_range_entity_id_does_not_break_the_list(entitled_client, session):
    """A TMDB id past int32 is the one malformed shape that is *syntactically* fine: it passes
    `normalise_entity_id`'s positive-integer check and `int()` parses it, so the guard that
    catches `"not-a-number"` lets it through — and the catalog's keys are `Integer`. Before this
    was bounded, such a row aborted the whole statement in the driver, taking every other follow
    on the list down with it."""
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
            entity_id="9999999999",
            source="letterboxd_import",
        )
    )
    await session.commit()

    r = await entitled_client.get("/me/follows")
    assert r.status_code == 200
    items = {f["entity_id"]: f for f in r.json()["items"]}
    assert items["9999999999"]["name"] is None
    assert items["287"]["name"] == "Brad Pitt"


async def test_two_spellings_of_one_id_both_get_the_name(entitled_client, session):
    """`normalise_entity_id` keeps the second spelling out of the table, but the importers write
    through the service, so both can be present. Labelling one and nulling the other would be
    the worse failure — it looks like the catalog is missing the entity."""
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
            entity_id="0287",
            source="letterboxd_import",
        )
    )
    await session.commit()

    r = await entitled_client.get("/me/follows")
    items = {f["entity_id"]: f for f in r.json()["items"]}
    assert set(items) == {"287", "0287"}
    assert items["287"]["name"] == "Brad Pitt"
    assert items["0287"]["name"] == "Brad Pitt"


async def test_a_title_follow_of_a_film_the_catalog_lost_is_listed_with_nulls(
    entitled_client, session, film
):
    """The UUID branch of the same guarantee: well-formed id, no such row."""
    from uuid import uuid4

    from sqlalchemy import select

    from upmovies.app.models import Follow

    await entitled_client.post(
        "/me/follows", json={"entity_type": "title", "entity_id": str(film.id)}
    )

    followed = (await session.execute(select(Follow))).scalars().first()
    assert followed is not None
    missing = str(uuid4())
    session.add(
        Follow(
            user_id=followed.user_id,
            entity_type="title",
            entity_id=missing,
            source="letterboxd_import",
        )
    )
    await session.commit()

    r = await entitled_client.get("/me/follows")
    assert r.status_code == 200
    items = {f["entity_id"]: f for f in r.json()["items"]}
    assert set(items) == {str(film.id), missing}
    assert (items[missing]["name"], items[missing]["image_path"]) == (None, None)
    assert items[str(film.id)]["name"] == film.title
