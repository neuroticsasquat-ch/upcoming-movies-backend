from upmovies.catalog.release_grade import release_bucket

# TMDB release_dates `type` ints (per /movie/{id}/release_dates):
#   1 Premiere · 2 Theatrical (limited) · 3 Theatrical (wide) · 4 Digital · 5 Physical · 6 TV
# This site surfaces the theatrical arc — wide (3) + limited (2), US or origin country — plus
# the US home release, digital (4) + physical (5) (D-26). Premiere (1) is excluded — TMDB has no
# distinct "festival" type (type 1 lumps real festival screenings with ordinary premieres,
# distinguishable only by free-text `note`), so we drop it rather than mislabel; TV (6) is
# nobody's release date. Membership — including the home release's US-only region rule — is
# `catalog.release_grade`'s to decide; this module only labels what it admits. The same rule
# drives both the movie page release list and the /calendar feed.
RELEASE_BUCKETS: tuple[str, ...] = (
    "limited",
    "wide",
    "digital",
    "physical",
)  # display + significance order

# Human-readable label per bucket, for the movie page's "Release dates" section — where the
# section heading already says "Release", so the bucket label drops the redundant word.
# The frontend calendar intentionally keeps the longer "Limited release" / "Wide release"
# (RELEASE_BUCKET_LABELS in components/calendar/release-labels.ts), since it has no such heading.
RELEASE_BUCKET_LABELS: dict[str, str] = {
    "limited": "Limited",
    "wide": "Wide",
    "digital": "Digital",
    "physical": "Physical",
}


def bucket_for_tmdb_type(tmdb_type: int) -> str | None:
    """The display bucket for a TMDB release `type`, or None if not surfaced."""
    return release_bucket(tmdb_type)


def release_label_for_tmdb_type(tmdb_type: int) -> str | None:
    """The human-readable release label for a surfaced `type`, or None.

    Type only — a caller deciding whether to *list* a row must first pass it through
    `catalog.release_grade.is_displayable_release`, which is what keeps a non-US digital date
    off the page.
    """
    bucket = release_bucket(tmdb_type)
    return RELEASE_BUCKET_LABELS[bucket] if bucket is not None else None
