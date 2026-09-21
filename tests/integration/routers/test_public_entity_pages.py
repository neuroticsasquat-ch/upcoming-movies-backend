"""`GET /companies/{ref}` and `GET /collections/{ref}` — the studio and franchise pages
(NEU-1428, EF-17).

`test_public_person.py`'s two lists without its credits and tier, so the assertions that matter
most are the same ones: what is *not* returned. Both types are driven through the same
parametrized body wherever the rule is shared, because "same shape as the person page" is the
whole contract and two hand-written copies would be free to drift apart.
"""

from datetime import UTC, datetime, timedelta

import pytest

TODAY = datetime.now(tz=UTC).date()
MAX_AGE_DAYS = 365
"""`PROVIDER_POLL_MAX_AGE_DAYS`' default, which the `recent` list's window rides on."""


@pytest.fixture
def make_entity(make_company, make_collection, session):
    """Mint one studio or franchise and return `(path, attach)` for it.

    `attach` puts a film in it — a join row for a studio, a column on the film for a franchise,
    which is the one thing the two types genuinely disagree about.
    """

    async def _make(kind: str, *, id: int, name: str):
        if kind == "company":
            await make_company(id=id, name=name, logo_path="/logo.jpg")

            async def attach(film):
                from upmovies.catalog.models import FilmProductionCompany

                session.add(FilmProductionCompany(film_id=film.id, company_id=id))
                await session.commit()

            return "companies", attach

        await make_collection(id=id, name=name, poster_path="/poster.jpg")

        async def attach(film):
            film.collection_id = id
            await session.commit()

        return "collections", attach

    return _make


KINDS = ("company", "collection")


@pytest.mark.parametrize("kind", KINDS)
async def test_the_page_answers_its_canonical_ref(client, make_entity, kind):
    """The ref resolves on the leading id and the response carries the canonical spelling —
    the client redirects when the two differ, exactly as the person page does."""
    path, _ = await make_entity(kind, id=174, name="Warner Bros. Pictures")

    for ref in ("174", "174-anything-at-all", "174-warner-bros-pictures"):
        r = await client.get(f"/{path}/{ref}")
        assert r.status_code == 200, f"ref={ref!r} → {r.status_code}"
        assert r.json()["ref"] == "174-warner-bros-pictures"


@pytest.mark.parametrize("kind", KINDS)
async def test_an_unknown_entity_is_404(client, make_entity, kind):
    path, _ = await make_entity(kind, id=174, name="Warner Bros. Pictures")

    assert (await client.get(f"/{path}/999")).status_code == 404


@pytest.mark.parametrize("kind", KINDS)
async def test_a_ref_that_does_not_lead_with_an_id_is_404(client, make_entity, kind):
    path, _ = await make_entity(kind, id=174, name="Warner Bros. Pictures")

    assert (await client.get(f"/{path}/warner-bros-pictures")).status_code == 404


async def test_entity_search_still_routes(client, make_company, make_collection):
    """`/companies/search` and `/collections/search` are literal paths registered before
    `{ref}`, and a page route that swallowed them would take the header's search box — and the
    film page's follow buttons — down with it."""
    await make_company(id=174, name="Warner Bros. Pictures")
    await make_collection(id=263, name="The Dark Knight Collection")

    assert (await client.get("/companies/search", params={"q": "warner"})).status_code == 200
    assert (await client.get("/collections/search", params={"q": "knight"})).status_code == 200


# ── the headers ───────────────────────────────────────────────────────────────


async def test_a_studio_carries_its_logo(client, make_company):
    await make_company(id=174, name="Warner Bros. Pictures", logo_path="/wb.jpg")

    body = (await client.get("/companies/174")).json()
    assert body["id"] == 174
    assert body["name"] == "Warner Bros. Pictures"
    assert body["logo_path"] == "/wb.jpg"


async def test_a_franchise_carries_its_poster(client, make_collection):
    await make_collection(id=263, name="The Dark Knight Collection", poster_path="/tdk.jpg")

    body = (await client.get("/collections/263")).json()
    assert body["id"] == 263
    assert body["name"] == "The Dark Knight Collection"
    assert body["poster_path"] == "/tdk.jpg"


# ── the two lists ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
async def test_films_split_into_upcoming_and_recent_and_never_both(
    client, make_entity, make_film, kind
):
    """`upcoming` is in play, `recent` is the alert window less in play. A film is in one or
    the other, and a film past the window is in neither — that set is exactly what a follow can
    reach (D-46)."""
    path, attach = await make_entity(kind, id=174, name="Warner Bros. Pictures")
    for slug, title, release_date in (
        ("upcoming", "Upcoming", TODAY + timedelta(days=30)),
        ("recent", "Recent", TODAY - timedelta(days=30)),
        ("old", "Old", TODAY - timedelta(days=MAX_AGE_DAYS + 1)),
    ):
        await attach(await make_film(slug=slug, title=title, release_date=release_date))

    body = (await client.get(f"/{path}/174")).json()
    assert [i["title"] for i in body["upcoming"]] == ["Upcoming"]
    assert [i["title"] for i in body["recent"]] == ["Recent"]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_row_carries_no_credits(client, make_entity, make_film, kind):
    """The studio and franchise rows are the person page's row *without* `credits` (EF-17):
    neither type has a job to name. Neither carries a `tier` — but nor does the person row any
    more (EF-1), so that assertion is now about the shape being bare, not about the
    difference."""
    path, attach = await make_entity(kind, id=174, name="Warner Bros. Pictures")
    await attach(await make_film(slug="only", title="Only", release_date=None))

    (row,) = (await client.get(f"/{path}/174")).json()["upcoming"]
    assert "credits" not in row
    assert "tier" not in row
    assert set(row) == {"ref", "id", "tmdb_id", "slug", "title", "poster_path", "headline_release"}


@pytest.mark.parametrize("kind", KINDS)
async def test_a_row_cites_the_date_the_film_page_shows(
    client, make_entity, make_film, add_release_date, kind
):
    """`headline_release`, not `catalog.film.release_date` — the watchlist row's rule
    (NEU-1397), inherited by sharing its shape rather than restated."""
    path, attach = await make_entity(kind, id=174, name="Warner Bros. Pictures")
    film = await make_film(slug="dated", title="Dated", release_date=TODAY + timedelta(days=30))
    await add_release_date(
        film=film, release_date=datetime(TODAY.year + 1, 5, 1, tzinfo=UTC), release_type=3
    )
    await attach(film)

    (row,) = (await client.get(f"/{path}/174")).json()["upcoming"]
    assert row["headline_release"]["date"] == f"{TODAY.year + 1}-05-01"
    assert row["ref"] == f"{film.tmdb_id}-dated"


@pytest.mark.parametrize("kind", KINDS)
async def test_upcoming_runs_soonest_first_and_recent_newest_first(
    client, make_entity, make_film, kind
):
    """The person page's ordering, which is what "same ordering as the person page" buys: the
    two lists run away from today in both directions."""
    path, attach = await make_entity(kind, id=174, name="Warner Bros. Pictures")
    for slug, title, days in (
        ("soon", "Soon", 10),
        ("later", "Later", 200),
        ("just-out", "Just Out", -10),
        ("a-while-ago", "A While Ago", -200),
    ):
        await attach(
            await make_film(slug=slug, title=title, release_date=TODAY + timedelta(days=days))
        )

    body = (await client.get(f"/{path}/174")).json()
    assert [i["title"] for i in body["upcoming"]] == ["Soon", "Later"]
    assert [i["title"] for i in body["recent"]] == ["Just Out", "A While Ago"]


@pytest.mark.parametrize("kind", KINDS)
async def test_an_undated_film_is_upcoming_and_sorts_last(client, make_entity, make_film, kind):
    """An undated film is still in play, and it is the least certain thing on the page whichever
    direction the dates run — so it sits at the end of the list, not the front."""
    path, attach = await make_entity(kind, id=174, name="Warner Bros. Pictures")
    await attach(await make_film(slug="undated", title="Undated", release_date=None))
    await attach(await make_film(slug="dated", title="Dated", release_date=TODAY + timedelta(30)))

    body = (await client.get(f"/{path}/174")).json()
    assert [i["title"] for i in body["upcoming"]] == ["Dated", "Undated"]
    assert body["recent"] == []


@pytest.mark.parametrize("kind", KINDS)
async def test_an_entity_with_nothing_in_reach_gets_two_empty_lists(client, make_entity, kind):
    """An empty page is a real answer — the client renders "No upcoming films in the
    catalog" — and is not the same as a 404."""
    path, _ = await make_entity(kind, id=174, name="Nothing Yet")

    body = (await client.get(f"/{path}/174")).json()
    assert body["upcoming"] == []
    assert body["recent"] == []


@pytest.mark.parametrize("kind", KINDS)
async def test_another_entitys_films_are_not_listed(client, make_entity, make_film, kind):
    path, attach = await make_entity(kind, id=174, name="Warner Bros. Pictures")
    _, other_attach = await make_entity(kind, id=175, name="Somebody Else")
    await attach(await make_film(slug="ours", title="Ours", release_date=None))
    await other_attach(await make_film(slug="theirs", title="Theirs", release_date=None))

    body = (await client.get(f"/{path}/174")).json()
    assert [i["title"] for i in body["upcoming"]] == ["Ours"]


async def test_a_film_with_two_studios_appears_on_both_pages(client, make_entity, make_film):
    """A co-production is a real row on each studio's page — `film_production_company` is a
    join table, unlike a franchise, which a film has at most one of."""
    path, warner = await make_entity("company", id=174, name="Warner Bros. Pictures")
    _, legendary = await make_entity("company", id=923, name="Legendary Pictures")
    film = await make_film(slug="shared", title="Shared", release_date=None)
    await warner(film)
    await legendary(film)

    for company_id in (174, 923):
        body = (await client.get(f"/{path}/{company_id}")).json()
        assert [i["title"] for i in body["upcoming"]] == ["Shared"]
