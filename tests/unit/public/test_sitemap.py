"""Unit tests: render_sitemap derivation proof for backlotter.com base URL.

These tests are pure (no DB, no settings) — they prove that render_sitemap
emits the expected <loc>s for any given base_url, including the prod value.
"""

from datetime import UTC, datetime
from xml.etree import ElementTree

import pytest

from upmovies.public.service import SitemapEntity, SitemapFilm
from upmovies.public.sitemap import render_sitemap

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

_LASTMOD = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


def _locs(xml_text: str) -> list[str | None]:
    root = ElementTree.fromstring(xml_text)
    return [el.text for el in root.findall(".//sm:url/sm:loc", _NS)]


def test_render_sitemap_backlotter_root_loc():
    """Root <loc> must be https://backlotter.com/ (trailing slash, no double-slash)."""
    result = render_sitemap("https://backlotter.com", [], [])
    locs = _locs(result)
    assert "https://backlotter.com/" in locs


def test_render_sitemap_backlotter_film_loc():
    """Per-film <loc> must be https://backlotter.com/film/{ref}."""
    films = [SitemapFilm(ref="945961-alien-romulus", lastmod=_LASTMOD)]
    result = render_sitemap("https://backlotter.com", films, [])
    locs = _locs(result)
    assert "https://backlotter.com/film/945961-alien-romulus" in locs


def test_render_sitemap_backlotter_multiple_films():
    """All film <loc>s appear on backlotter.com."""
    films = [
        SitemapFilm(ref="1-film-one", lastmod=_LASTMOD),
        SitemapFilm(ref="2-film-two", lastmod=_LASTMOD),
    ]
    result = render_sitemap("https://backlotter.com", films, [])
    locs = _locs(result)
    assert "https://backlotter.com/film/1-film-one" in locs
    assert "https://backlotter.com/film/2-film-two" in locs


@pytest.mark.parametrize(
    "base_url",
    [
        "https://backlotter.com/",  # trailing slash — must not produce double slash
        "https://backlotter.com",  # canonical — baseline
    ],
)
def test_render_sitemap_trailing_slash_base_no_double_slash(base_url: str):
    """rstrip('/') must prevent // in any <loc> regardless of trailing slash on base."""
    films = [SitemapFilm(ref="3-some-film", lastmod=_LASTMOD)]
    result = render_sitemap(base_url, films, [])
    assert "//" not in result.replace("https://", "").replace("http://", "")
    locs = _locs(result)
    assert "https://backlotter.com/" in locs
    assert "https://backlotter.com/film/3-some-film" in locs


def test_render_sitemap_entity_locs():
    """Person, studio and franchise pages sit on the frontend's own route segments — EF-19's
    on-screen vocabulary, not the code's `company` / `collection`."""
    entities = [
        SitemapEntity(path="person", ref="525-christopher-nolan"),
        SitemapEntity(path="studio", ref="174-warner-bros-pictures"),
        SitemapEntity(path="franchise", ref="263-the-dark-knight-collection"),
    ]
    locs = _locs(render_sitemap("https://backlotter.com", [], entities))
    assert "https://backlotter.com/person/525-christopher-nolan" in locs
    assert "https://backlotter.com/studio/174-warner-bros-pictures" in locs
    assert "https://backlotter.com/franchise/263-the-dark-knight-collection" in locs


def test_render_sitemap_entities_carry_no_lastmod():
    """An entity page's freshness is not a timestamp this schema holds, and an invented
    `lastmod` teaches the crawler to ignore the field — so only the film rows carry one."""
    xml = render_sitemap(
        "https://backlotter.com",
        [SitemapFilm(ref="1-film-one", lastmod=_LASTMOD)],
        [SitemapEntity(path="person", ref="525-christopher-nolan")],
    )
    root = ElementTree.fromstring(xml)
    with_lastmod = [
        loc.text
        for el in root.findall(".//sm:url", _NS)
        if el.find("sm:lastmod", _NS) is not None
        for loc in el.findall("sm:loc", _NS)
    ]
    assert with_lastmod == ["https://backlotter.com/film/1-film-one"]
