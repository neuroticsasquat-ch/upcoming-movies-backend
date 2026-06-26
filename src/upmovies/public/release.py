RELEASE_TYPE_LABELS: dict[int, str] = {
    1: "Premiere",
    2: "Theatrical (limited)",
    3: "Theatrical",
    4: "Digital",
    5: "Physical",
    6: "TV",
}


def release_type_label(t: int) -> str:
    return RELEASE_TYPE_LABELS.get(t, f"Unknown ({t})")


# TMDB release_dates `type` ints (per /movie/{id}/release_dates):
#   1 Premiere · 2 Theatrical (limited) · 3 Theatrical (wide) · 4 Digital · 5 Physical · 6 TV
# Calendar (M6) surfaces only the theatrical/premiere arc; festival ≈ Premiere (TMDB has no
# distinct festival type — it's type 1 distinguishable only by free-text `note`), so festival
# collapses into `premiere`. 4/5/6 are excluded in v1 (one-line widening to add later).
RELEASE_BUCKETS: tuple[str, ...] = ("premiere", "limited", "wide")  # display + significance order
_TMDB_TYPE_TO_BUCKET: dict[int, str] = {1: "premiere", 2: "limited", 3: "wide"}


def bucket_for_tmdb_type(tmdb_type: int) -> str | None:
    """The calendar display bucket for a TMDB release `type`, or None if not surfaced."""
    return _TMDB_TYPE_TO_BUCKET.get(tmdb_type)
