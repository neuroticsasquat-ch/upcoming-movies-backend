"""Which `catalog.film_release_date` rows are **displayable**: the theatrical arc in US or
origin country, plus the US home release.

This lives in `catalog` for the reason `seed_grade` does — there is more than one consumer and
they must not drift. Three ask this question today:

- `public.service` renders the film page's "Release dates" section from it;
- `public.service._region_visible` decides whether a `release_date` event reaches a surface;
- `ingest.tmdb.release_date_history` decides which changes are worth recording at all (NEU-1121).

Before this module the first two already disagreed: the page built its region set from
`origin_country[0]` (the first origin only) while the visibility predicate tested
`Event.region == any_(Film.origin_country)` (all of them). A film with two origin countries
could therefore show one date and surface an event about another. One definition, one place.

**Two cuts, not one.** TMDB release `type` ints are 1 Premiere · 2 Theatrical (limited) ·
3 Theatrical (wide) · 4 Digital · 5 Physical · 6 TV. The theatrical arc is 2 and 3, displayable
in US *or* an origin country. The home release is 4 and 5, displayable in **US only** (D-26):
the product answers "when can I watch this at home?" for a US audience, and a French digital
date is not that answer even for a French film — where a French *theatrical* date genuinely is
the film's own market opening. Premiere is excluded from both deliberately: TMDB has no distinct
festival type, so type 1 lumps real festival screenings with ordinary premieres and telling them
apart means parsing free-text `note`. TV (6) is nobody's release date. The display labels for
these buckets stay in `public.release` — they are a presentation concern; membership is not.

Widening this cut is how home-release dates reach the product at all: the film page, the
calendar, `_region_visible` and the change history all read this module, so a US digital date
becomes listable, carded and calendar-visible in one edit. There is **no backfill** — the first
observation after deploy is a baseline, not a change (ADR-0014), so no film cards a digital date
it already had.

**Why the primary date is not here.** `catalog.film.release_date` is TMDB's primary — the
earliest release in *any* country of *any* type — so it is routinely a date this cut excludes.
It survives as the year parenthetical after the film's title and nothing else, and it raises no
events (NEU-1121). Anything reaching for "the film's release date" wants this module instead —
and a surface with room for exactly one date wants `catalog.headline_release`, which chooses
among the governing dates this cut defines and reaches for the primary only when there is
nothing displayable to show.
"""

from collections.abc import Sequence

# TMDB release `type` ints that make up the theatrical arc: limited (2) and wide (3).
THEATRICAL_RELEASE_TYPES: frozenset[int] = frozenset({2, 3})

# TMDB release `type` ints that make up the home release: digital (4) and physical (5).
# US only — see the module docstring.
HOME_RELEASE_TYPES: frozenset[int] = frozenset({4, 5})

# The one region always in scope, whatever the film's origin — and the *only* region the home
# release is in scope for.
PRIMARY_REGION = "US"

# The bucket each displayable type belongs to. Lowercase because these are *identifiers* — they
# key `subject_key` tokens (`US:wide`, `US:digital`) and event bodies; the capitalized display
# forms live in `public.release`, which is where presentation belongs.
RELEASE_TYPE_BUCKETS: dict[int, str] = {2: "limited", 3: "wide", 4: "digital", 5: "physical"}


def release_bucket(release_type: int) -> str | None:
    """The bucket identifier for a TMDB release `type`, or None if not a displayable type.

    Type alone: this answers "which bucket is this?", not "is this row displayable?" — the
    home-release buckets are US-only and the region test lives in `is_displayable_release`.
    """
    return RELEASE_TYPE_BUCKETS.get(release_type)


def displayable_regions(origin_country: Sequence[str] | None) -> frozenset[str]:
    """The ISO 3166-1 alpha-2 regions whose *theatrical* dates this site surfaces for a film.

    The home release does not use this set — it is `PRIMARY_REGION` only, whatever the film's
    origin (see `is_displayable_release`). A caller filtering rows of every type wants that
    predicate, not this set.

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
    """Whether one release row is the kind this site shows, and so the kind worth carding.

    The two cuts take different regions: theatrical is US-or-origin, the home release is US and
    nothing else (D-26). An origin-country digital date fails here, so it never lists on the
    film page, never enters `film_release_date_change`, and never cards.
    """
    if release_type in HOME_RELEASE_TYPES:
        return iso_3166_1 == PRIMARY_REGION
    return release_type in THEATRICAL_RELEASE_TYPES and iso_3166_1 in displayable_regions(
        origin_country
    )
