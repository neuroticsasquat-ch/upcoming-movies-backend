"""Which `catalog.film_release_date` rows are **displayable**: US or origin country, theatrical.

This lives in `catalog` for the reason `seed_grade` does — there is more than one consumer and
they must not drift. Three ask this question today:

- `public.service` renders the film page's "Release dates" section from it;
- `public.service._region_visible` decides whether a `release_date` event reaches a surface;
- `ingest.tmdb.release_date_history` decides which changes are worth recording at all (NEU-1121).

Before this module the first two already disagreed: the page built its region set from
`origin_country[0]` (the first origin only) while the visibility predicate tested
`Event.region == any_(Film.origin_country)` (all of them). A film with two origin countries
could therefore show one date and surface an event about another. One definition, one place.

**Theatrical only.** TMDB release `type` ints are 1 Premiere · 2 Theatrical (limited) ·
3 Theatrical (wide) · 4 Digital · 5 Physical · 6 TV. Only 2 and 3 are the theatrical arc this
site tracks. Premiere is excluded deliberately: TMDB has no distinct festival type, so type 1
lumps real festival screenings with ordinary premieres and telling them apart means parsing
free-text `note`. The display labels for these buckets stay in `public.release` — they are a
presentation concern; membership is not.

**Why the primary date is not here.** `catalog.film.release_date` is TMDB's primary — the
earliest release in *any* country of *any* type — so it is routinely a date this cut excludes.
It survives as the year parenthetical after the film's title and nothing else, and it raises no
events (NEU-1121). Anything reaching for "the film's release date" wants this module instead.
"""

from collections.abc import Sequence

# TMDB release `type` ints that make up the theatrical arc: limited (2) and wide (3).
THEATRICAL_RELEASE_TYPES: frozenset[int] = frozenset({2, 3})

# The one region always in scope, whatever the film's origin.
PRIMARY_REGION = "US"

# The bucket each theatrical type belongs to. Lowercase because these are *identifiers* — they
# key `subject_key` tokens (`US:wide`) and event bodies; the capitalized display forms live in
# `public.release`, which is where presentation belongs.
RELEASE_TYPE_BUCKETS: dict[int, str] = {2: "limited", 3: "wide"}


def release_bucket(release_type: int) -> str | None:
    """The bucket identifier for a TMDB release `type`, or None if not a theatrical type."""
    return RELEASE_TYPE_BUCKETS.get(release_type)


def displayable_regions(origin_country: Sequence[str] | None) -> frozenset[str]:
    """The ISO 3166-1 alpha-2 regions whose release dates this site surfaces for a film.

    `US` plus **every** origin country, not just the first: a co-production carries several and
    a date in any of them is as much "the film's own market" as a date in the first-listed one.
    This is the wider of the two readings that were live before NEU-1121, chosen because the
    narrower one silently hid dates for co-productions.
    """
    regions = {PRIMARY_REGION}
    if origin_country:
        regions.update(c for c in origin_country if c)
    return frozenset(regions)


def is_displayable_release(
    *, iso_3166_1: str, release_type: int, origin_country: Sequence[str] | None
) -> bool:
    """Whether one release row is the kind this site shows, and so the kind worth carding."""
    return release_type in THEATRICAL_RELEASE_TYPES and iso_3166_1 in displayable_regions(
        origin_country
    )
