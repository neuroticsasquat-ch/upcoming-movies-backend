from datetime import date
from uuid import uuid4

from upmovies.catalog.slug import backfill_slugs, base_slug, resolve_unique


def test_base_slug_title_and_year():
    assert base_slug("The Odyssey", date(2026, 7, 15), 1) == "the-odyssey-2026"


def test_base_slug_transliterates_non_ascii():
    assert base_slug("Amélie", date(2001, 4, 25), 2) == "amelie-2001"


def test_base_slug_strips_punctuation():
    assert (
        base_slug("Spider-Man: No Way Home", date(2021, 12, 17), 3) == "spider-man-no-way-home-2021"
    )


def test_base_slug_without_release_date_omits_year():
    assert base_slug("The Odyssey", None, 4) == "the-odyssey"


def test_base_slug_empty_stem_falls_back_to_tmdb_id():
    # an all-punctuation / untransliterable title slugifies to "" → deterministic unique fallback
    assert base_slug("!!!", date(2026, 1, 1), 99) == "film-99"


def test_resolve_unique_passes_through_when_free():
    assert resolve_unique("dune-2026", set(), 5) == "dune-2026"


def test_resolve_unique_appends_tmdb_id_on_clash():
    assert resolve_unique("dune-2026", {"dune-2026"}, 5) == "dune-2026-5"


def test_backfill_slugs_assigns_unique_slugs_in_tmdb_order():
    a, b, c = uuid4(), uuid4(), uuid4()
    rows = [
        (a, "Dune", date(2026, 7, 15), 1),
        (b, "Dune", date(2026, 7, 15), 2),  # same base as `a` → disambiguated by tmdb_id
        (c, "Wicked", None, 3),
    ]
    out = dict(backfill_slugs(rows))
    assert out[a] == "dune-2026"
    assert out[b] == "dune-2026-2"
    assert out[c] == "wicked"
    assert len(set(out.values())) == 3  # all unique
