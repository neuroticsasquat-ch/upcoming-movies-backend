"""Unit tests: render_sitemap derivation proof for backlotter.com base URL.

These tests are pure (no DB, no settings) — they prove that render_sitemap
emits the expected <loc>s for any given base_url, including the prod value.
"""

from datetime import UTC, datetime
from xml.etree import ElementTree

import pytest

from upmovies.public.service import SitemapFilm
from upmovies.public.sitemap import render_sitemap

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

_LASTMOD = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


def _locs(xml_text: str) -> list[str | None]:
    root = ElementTree.fromstring(xml_text)
    return [el.text for el in root.findall(".//sm:url/sm:loc", _NS)]


def test_render_sitemap_backlotter_root_loc():
    """Root <loc> must be https://backlotter.com/ (trailing slash, no double-slash)."""
    result = render_sitemap("https://backlotter.com", [])
    locs = _locs(result)
    assert "https://backlotter.com/" in locs


def test_render_sitemap_backlotter_film_loc():
    """Per-film <loc> must be https://backlotter.com/film/{slug}."""
    films = [SitemapFilm(slug="alien-romulus-2024", lastmod=_LASTMOD)]
    result = render_sitemap("https://backlotter.com", films)
    locs = _locs(result)
    assert "https://backlotter.com/film/alien-romulus-2024" in locs


def test_render_sitemap_backlotter_multiple_films():
    """All film <loc>s appear on backlotter.com."""
    films = [
        SitemapFilm(slug="film-one-2025", lastmod=_LASTMOD),
        SitemapFilm(slug="film-two-2025", lastmod=_LASTMOD),
    ]
    result = render_sitemap("https://backlotter.com", films)
    locs = _locs(result)
    assert "https://backlotter.com/film/film-one-2025" in locs
    assert "https://backlotter.com/film/film-two-2025" in locs


@pytest.mark.parametrize(
    "base_url",
    [
        "https://backlotter.com/",  # trailing slash — must not produce double slash
        "https://backlotter.com",  # canonical — baseline
    ],
)
def test_render_sitemap_trailing_slash_base_no_double_slash(base_url: str):
    """rstrip('/') must prevent // in any <loc> regardless of trailing slash on base."""
    films = [SitemapFilm(slug="some-film-2025", lastmod=_LASTMOD)]
    result = render_sitemap(base_url, films)
    assert "//" not in result.replace("https://", "").replace("http://", "")
    locs = _locs(result)
    assert "https://backlotter.com/" in locs
    assert "https://backlotter.com/film/some-film-2025" in locs
