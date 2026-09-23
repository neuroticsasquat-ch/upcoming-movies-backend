from datetime import UTC, date, datetime
from xml.etree import ElementTree

from tests.fixtures.public import ref
from upmovies.config import get_settings

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


async def test_sitemap_lists_indexed_films_plus_root(client, make_film, add_event):
    shown = await make_film(slug="shown-2026")
    await add_event(film=shown, summary="s", occurred_at=datetime(2025, 5, 1, tzinfo=UTC))
    bare = await make_film(slug="bare-2026")  # no summarized event -> excluded

    r = await client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")

    root = ElementTree.fromstring(r.text)
    locs = [el.text for el in root.findall(".//sm:url/sm:loc", _NS)]
    assert "http://localhost:5173/" in locs
    assert f"http://localhost:5173/film/{ref(shown)}" in locs
    assert all(ref(bare) not in (loc or "") for loc in locs)


async def test_sitemap_lastmod_is_a_valid_date(client, make_film, add_event):
    film = await make_film(slug="dated-2026")
    await add_event(film=film, summary="s")

    r = await client.get("/sitemap.xml")
    root = ElementTree.fromstring(r.text)
    lastmods = [el.text for el in root.findall(".//sm:url/sm:lastmod", _NS)]
    assert len(lastmods) == 1
    assert lastmods[0] == date.today().isoformat()


async def test_sitemap_excludes_film_with_only_other_events(client, make_film, add_event):
    other_only = await make_film(slug="otheronly-sitemap-2026")
    await add_event(film=other_only, event_type="other", summary="s")

    r = await client.get("/sitemap.xml")
    assert "otheronly-sitemap-2026" not in r.text


async def test_sitemap_uses_overridden_base_url(client, make_film, add_event, monkeypatch):
    """Prove the wiring: when get_settings returns backlotter.com, <loc>s use that origin."""
    film = await make_film(slug="wired-film-2026")
    await add_event(film=film, summary="s")

    base_settings = get_settings()
    monkeypatch.setattr(
        "upmovies.routers.public.get_settings",
        lambda: base_settings.model_copy(update={"public_base_url": "https://backlotter.com"}),
    )

    r = await client.get("/sitemap.xml")
    assert r.status_code == 200

    root = ElementTree.fromstring(r.text)
    locs = [el.text for el in root.findall(".//sm:url/sm:loc", _NS)]
    assert "https://backlotter.com/" in locs
    assert f"https://backlotter.com/film/{ref(film)}" in locs
    assert not any("localhost" in (loc or "") for loc in locs)


# ── entity pages (NEU-1428, EF-17) ────────────────────────────────────────────


async def test_sitemap_lists_entities_that_have_a_film_in_reach(
    client, session, make_film, make_person, make_company, make_collection
):
    """Person, studio and franchise pages join the sitemap when they have something to show —
    the set their own page renders."""
    from tests.fixtures.catalog import add_credit
    from upmovies.catalog.models import FilmProductionCompany

    film = await make_film(slug="in-reach-2026", release_date=date.today())
    await make_person(id=525, name="Christopher Nolan")
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await make_company(id=174, name="Warner Bros. Pictures")
    session.add(FilmProductionCompany(film_id=film.id, company_id=174))
    await make_collection(id=263, name="The Dark Knight Collection")
    film.collection_id = 263
    await session.commit()

    root = ElementTree.fromstring((await client.get("/sitemap.xml")).text)
    locs = [el.text for el in root.findall(".//sm:url/sm:loc", _NS)]
    assert "http://localhost:5173/person/525-christopher-nolan" in locs
    assert "http://localhost:5173/studio/174-warner-bros-pictures" in locs
    assert "http://localhost:5173/franchise/263-the-dark-knight-collection" in locs


async def test_sitemap_omits_entities_with_nothing_in_reach(
    client, make_person, make_company, make_collection
):
    """A sitemap is a claim that the URLs on it are worth fetching, and a page whose whole
    content is "No upcoming films" is not — so an entity with no film in reach stays off it."""
    await make_person(id=525, name="Christopher Nolan")
    await make_company(id=174, name="Warner Bros. Pictures")
    await make_collection(id=263, name="The Dark Knight Collection")

    body = (await client.get("/sitemap.xml")).text
    assert "/person/525" not in body
    assert "/studio/174" not in body
    assert "/franchise/263" not in body


async def test_sitemap_omits_a_person_tmdb_has_deleted(client, session, make_film, make_person):
    """`/people/{ref}` 404s for a tombstoned person, so listing the URL would submit a dead
    page — the same `_LIVE_PERSON` rule search and the onboarding grid apply."""
    from tests.fixtures.catalog import add_credit

    film = await make_film(slug="tombstone-2026", release_date=date.today())
    await make_person(
        id=525, name="Gone From TMDB", tmdb_missing_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await session.commit()

    assert "/person/525" not in (await client.get("/sitemap.xml")).text


async def test_sitemap_omits_an_entity_whose_films_are_past_the_window(
    client, session, make_film, make_company
):
    """The page's own bound (D-46): a studio whose only film left the alert window renders
    empty, so it leaves the sitemap with it."""
    from datetime import timedelta

    from upmovies.catalog.models import FilmProductionCompany

    film = await make_film(slug="ancient-2026", release_date=date.today() - timedelta(days=366))
    await make_company(id=174, name="Warner Bros. Pictures")
    session.add(FilmProductionCompany(film_id=film.id, company_id=174))
    await session.commit()

    assert "/studio/174" not in (await client.get("/sitemap.xml")).text
