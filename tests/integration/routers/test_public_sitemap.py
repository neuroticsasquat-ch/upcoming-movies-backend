from datetime import UTC, date, datetime
from xml.etree import ElementTree

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


async def test_sitemap_lists_indexed_films_plus_root(client, make_film, add_event):
    shown = await make_film(slug="shown-2026")
    await add_event(film=shown, summary="s", occurred_at=datetime(2025, 5, 1, tzinfo=UTC))
    await make_film(slug="bare-2026")  # no summarized event -> excluded

    r = await client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")

    root = ElementTree.fromstring(r.text)
    locs = [el.text for el in root.findall(".//sm:url/sm:loc", _NS)]
    assert "http://localhost:5173/" in locs
    assert "http://localhost:5173/film/shown-2026" in locs
    assert all("bare-2026" not in (loc or "") for loc in locs)


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
