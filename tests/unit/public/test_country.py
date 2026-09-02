from upmovies.public.country import COUNTRY_DISPLAY_NAMES, country_display_name


def test_mapped_code_uses_the_curated_display_name():
    assert country_display_name("US", "United States of America") == "USA"
    assert country_display_name("GB", "United Kingdom") == "UK"


def test_unmapped_code_falls_through_to_the_catalog_name():
    assert country_display_name("FR", "France") == "France"


def test_code_with_no_catalog_row_falls_through_to_the_raw_code():
    # A missing `catalog.production_country` row is a catalog gap, not a reason to drop a
    # co-production partner from the parenthetical.
    assert country_display_name("ZZ", None) == "ZZ"


def test_mapped_code_wins_even_when_the_catalog_row_is_missing():
    assert country_display_name("US", None) == "USA"


def test_map_is_keyed_by_uppercase_alpha_2_codes():
    assert all(len(code) == 2 and code.isupper() for code in COUNTRY_DISPLAY_NAMES)
