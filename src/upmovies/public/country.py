"""Display forms for ISO 3166-1 alpha-2 production-country codes.

Presentation labels live in `public/` while membership rules live in `catalog/` — the same
split `public/release.py` already draws for release buckets.
"""

# TMDB's own country names are usually fine in a title parenthetical; this map carries only the
# codes whose stored name is too long or too formal for a line that sits beside a film title
# (NEU-1215). Of the 118 countries in the catalog only 11 have names over 14 characters, so the
# map stays small by design. Anything absent falls through to `catalog.production_country.name`.
COUNTRY_DISPLAY_NAMES: dict[str, str] = {
    "US": "USA",
    "GB": "UK",
    "AE": "UAE",
    "BA": "Bosnia",
    "SY": "Syria",
    "KG": "Kyrgyzstan",
    "PS": "Palestine",
}


def country_display_name(iso_3166_1: str, catalog_name: str | None) -> str:
    """The display form for a country code, given its stored catalog name.

    Falls back to the catalog name, then to the raw code — a country with no
    `catalog.production_country` row is a catalog gap, not a reason to drop a co-production
    partner from the list.
    """
    mapped = COUNTRY_DISPLAY_NAMES.get(iso_3166_1)
    if mapped is not None:
        return mapped
    return catalog_name or iso_3166_1
