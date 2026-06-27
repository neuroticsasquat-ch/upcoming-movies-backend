# TMDB release_dates `type` ints (per /movie/{id}/release_dates):
#   1 Premiere · 2 Theatrical (limited) · 3 Theatrical (wide) · 4 Digital · 5 Physical · 6 TV
# This site surfaces ONLY the theatrical-arc dates we care about: wide (3) + limited (2).
# Premiere (1) is excluded — TMDB has no distinct "festival" type (type 1 lumps real festival
# screenings with ordinary premieres, distinguishable only by free-text `note`), so we drop it
# rather than mislabel. 4/5/6 are non-theatrical. Same rule drives both the movie page release
# list and the /calendar feed.
RELEASE_BUCKETS: tuple[str, ...] = ("limited", "wide")  # display + significance order
_TMDB_TYPE_TO_BUCKET: dict[int, str] = {2: "limited", 3: "wide"}

# Human-readable label per bucket, for the movie page's "Release dates" section — where the
# section heading already says "Release", so the bucket label drops the redundant word.
# The frontend calendar intentionally keeps the longer "Limited release" / "Wide release"
# (RELEASE_BUCKET_LABELS in components/calendar/release-labels.ts), since it has no such heading.
RELEASE_BUCKET_LABELS: dict[str, str] = {"limited": "Limited", "wide": "Wide"}


def bucket_for_tmdb_type(tmdb_type: int) -> str | None:
    """The display bucket for a TMDB release `type`, or None if not surfaced."""
    return _TMDB_TYPE_TO_BUCKET.get(tmdb_type)


def release_label_for_tmdb_type(tmdb_type: int) -> str | None:
    """The human-readable release label for a surfaced theatrical `type`, or None."""
    bucket = _TMDB_TYPE_TO_BUCKET.get(tmdb_type)
    return RELEASE_BUCKET_LABELS[bucket] if bucket is not None else None
