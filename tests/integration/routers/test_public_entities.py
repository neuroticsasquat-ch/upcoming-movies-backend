"""Public entity search: /people/search, /people/popular, /companies/search,
/collections/search (NEU-1350). Follow targets for the film page and the onboarding grid."""

from datetime import UTC, datetime

# ── /people/search ────────────────────────────────────────────────────────────


async def test_people_search_envelope(client, make_person):
    await make_person(id=1, name="Greta Gerwig", known_for_department="Directing")

    r = await client.get("/people/search", params={"q": "gerwig"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert body["items"] == [
        {
            "id": 1,
            "name": "Greta Gerwig",
            "known_for_department": "Directing",
            "profile_path": "/p.jpg",
        }
    ]


async def test_people_search_is_case_insensitive_substring_and_folds(client, make_person):
    person = await make_person(id=1, name="Penélope Cruz")
    await make_person(id=2, name="Someone Else")

    for q in ("penelope", "PENÉLOPE CRUZ", "lope cr", "penelopecruz"):
        r = await client.get("/people/search", params={"q": q})
        assert r.status_code == 200, f"q={q!r} → {r.status_code}"
        ids = [i["id"] for i in r.json()["items"]]
        assert ids == [person.id], f"q={q!r}: {ids}"


async def test_people_search_matches_original_name(client, make_person):
    person = await make_person(id=1, name="Bong Joon-ho", original_name="봉준호")
    await make_person(id=2, name="Other Person", original_name=None)

    r = await client.get("/people/search", params={"q": "봉준호"})
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [person.id]


async def test_people_search_orders_by_popularity_desc_nulls_last(client, make_person):
    await make_person(id=1, name="Chris Nobody", popularity=None)
    await make_person(id=2, name="Chris Pratt", popularity=40.0)
    await make_person(id=3, name="Chris Evans", popularity=80.0)
    await make_person(id=4, name="Chris Pine", popularity=40.0)

    r = await client.get("/people/search", params={"q": "chris"})
    assert r.status_code == 200
    # popularity desc; equal popularity ties break on id asc; NULL popularity sorts last.
    assert [i["id"] for i in r.json()["items"]] == [3, 2, 4, 1]


async def test_people_search_excludes_people_tmdb_has_deleted(client, make_person):
    live = await make_person(id=1, name="Chris Live")
    await make_person(id=2, name="Chris Gone", tmdb_missing_at=datetime(2026, 1, 1, tzinfo=UTC))

    r = await client.get("/people/search", params={"q": "chris"})
    assert r.status_code == 200
    body = r.json()
    assert [i["id"] for i in body["items"]] == [live.id]
    assert body["total"] == 1


async def test_people_search_short_query_returns_empty_page(client, make_person):
    await make_person(id=1, name="A")

    for q in ("", " ", "a", "%", "--"):
        r = await client.get("/people/search", params={"q": q})
        assert r.status_code == 200, f"q={q!r} → {r.status_code}"
        assert r.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}, f"q={q!r}"


async def test_people_search_ignores_punctuation_in_the_query(client, make_person):
    """LIKE wildcards never reach the database: the fold strips every non-alphanumeric from
    both sides, so "50%" is the query "50" and "_" matches nothing on its own."""
    match = await make_person(id=1, name="Agent 50")
    await make_person(id=2, name="Agent Plain")

    r = await client.get("/people/search", params={"q": "50%"})
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [match.id]

    r = await client.get("/people/search", params={"q": "a_e"})
    assert r.status_code == 200
    # "ae" is not a substring of either folded name.
    assert r.json()["items"] == []


async def test_people_search_pagination(client, make_person):
    for i in range(5):
        await make_person(id=i + 1, name=f"Chris Number {i}", popularity=float(100 - i))
    await make_person(id=99, name="Nobody Here", popularity=1000.0)

    r = await client.get("/people/search", params={"q": "chris", "limit": 2, "offset": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [i["id"] for i in body["items"]] == [1, 2]

    r = await client.get("/people/search", params={"q": "chris", "limit": 2, "offset": 4})
    body = r.json()
    assert body["total"] == 5
    assert [i["id"] for i in body["items"]] == [5]

    r = await client.get("/people/search", params={"q": "chris", "limit": 2, "offset": 10})
    assert r.json()["items"] == []


async def test_people_search_rejects_out_of_range_params(client):
    assert (
        await client.get("/people/search", params={"q": "chris", "limit": 0})
    ).status_code == 422
    assert (
        await client.get("/people/search", params={"q": "chris", "limit": 101})
    ).status_code == 422
    assert (
        await client.get("/people/search", params={"q": "chris", "offset": -1})
    ).status_code == 422
    assert (await client.get("/people/search", params={"q": "x" * 201})).status_code == 422
    assert (await client.get("/people/search")).status_code == 422


# ── /people/popular ───────────────────────────────────────────────────────────


async def test_popular_people_orders_by_popularity_and_defaults_to_thirty(client, make_person):
    for i in range(35):
        await make_person(id=i + 1, name=f"Person {i}", popularity=float(i))

    r = await client.get("/people/popular")
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 30
    ids = [i["id"] for i in body["items"]]
    assert len(ids) == 30
    assert ids == list(range(35, 5, -1))
    assert body["items"][0] == {
        "id": 35,
        "name": "Person 34",
        "known_for_department": "Acting",
        "profile_path": "/p.jpg",
    }


async def test_popular_people_honours_limit(client, make_person):
    for i in range(5):
        await make_person(id=i + 1, name=f"Person {i}", popularity=float(i))

    r = await client.get("/people/popular", params={"limit": 2})
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [5, 4]
    assert r.json()["limit"] == 2

    assert (await client.get("/people/popular", params={"limit": 0})).status_code == 422
    assert (await client.get("/people/popular", params={"limit": 101})).status_code == 422


async def test_popular_people_requires_a_photo_and_a_popularity(client, make_person):
    """The onboarding grid is a wall of faces (D-17): a person with no profile photo, no
    popularity score, or no TMDB entry any more cannot be on it."""
    shown = await make_person(id=1, name="Shown", popularity=10.0)
    await make_person(id=2, name="No Photo", popularity=50.0, profile_path=None)
    await make_person(id=3, name="No Score", popularity=None)
    await make_person(
        id=4, name="Gone", popularity=99.0, tmdb_missing_at=datetime(2026, 1, 1, tzinfo=UTC)
    )

    r = await client.get("/people/popular")
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [shown.id]


async def test_popular_people_empty_catalog(client):
    r = await client.get("/people/popular")
    assert r.status_code == 200
    assert r.json() == {"items": [], "limit": 30}


# ── /companies/search ─────────────────────────────────────────────────────────


async def test_companies_search_envelope_and_match(client, make_company):
    a24 = await make_company(id=41077, name="A24", logo_path="/a24.png", origin_country="US")
    await make_company(id=2, name="Warner Bros. Pictures", origin_country="US")

    r = await client.get("/companies/search", params={"q": "a24"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert body["items"] == [
        {"id": a24.id, "name": "A24", "logo_path": "/a24.png", "origin_country": "US"}
    ]


async def test_companies_search_folds_punctuation_and_orders_by_name(client, make_company):
    await make_company(id=3, name="Warner Bros. Television")
    await make_company(id=1, name="Warner Bros. Pictures")
    await make_company(id=2, name="Warner Bros. Animation")
    await make_company(id=4, name="Universal Pictures")

    r = await client.get("/companies/search", params={"q": "warnerbros"})
    assert r.status_code == 200
    assert [i["name"] for i in r.json()["items"]] == [
        "Warner Bros. Animation",
        "Warner Bros. Pictures",
        "Warner Bros. Television",
    ]


async def test_companies_search_pagination_and_short_query(client, make_company):
    for i in range(3):
        await make_company(id=i + 1, name=f"Studio {i}")

    r = await client.get("/companies/search", params={"q": "studio", "limit": 2, "offset": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert [i["id"] for i in body["items"]] == [3]

    r = await client.get("/companies/search", params={"q": "s"})
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}

    assert (
        await client.get("/companies/search", params={"q": "studio", "limit": 101})
    ).status_code == 422


# ── /collections/search ───────────────────────────────────────────────────────


async def test_collections_search_envelope_and_match(client, make_collection):
    col = await make_collection(
        id=10, name="The Lord of the Rings Collection", poster_path="/l.jpg"
    )
    await make_collection(id=11, name="The Hobbit Collection")

    r = await client.get("/collections/search", params={"q": "lord of the rings"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"] == [
        {"id": col.id, "name": "The Lord of the Rings Collection", "poster_path": "/l.jpg"}
    ]


async def test_collections_search_orders_by_name_and_paginates(client, make_collection):
    await make_collection(id=3, name="Mission: Impossible Collection")
    await make_collection(id=1, name="Mission to Mars Collection")
    await make_collection(id=2, name="Mission Collection")

    r = await client.get("/collections/search", params={"q": "mission"})
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [2, 3, 1]

    r = await client.get("/collections/search", params={"q": "mission", "limit": 1, "offset": 1})
    body = r.json()
    assert body["total"] == 3
    assert [i["id"] for i in body["items"]] == [3]

    r = await client.get("/collections/search", params={"q": "m"})
    assert r.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
