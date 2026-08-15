from collections.abc import Iterable

ARC_STAGES: tuple[str, ...] = (
    "announced",
    "cast",
    "shooting",
    "wrapped",
    "dated",
    "trailer",
    "released",
)

_RANK: dict[str, int] = {stage: index for index, stage in enumerate(ARC_STAGES)}

_STATUS_BASELINE: dict[str, str] = {
    "Rumored": "announced",
    "Planned": "announced",
    "In Production": "shooting",
    "Post Production": "wrapped",
    "Released": "released",
}

_EVENT_STAGE: dict[str, str] = {
    "announced": "announced",
    "casting": "cast",
    # A director or writer attaching is the "this is real now" beat, which is what `announced`
    # means here. Unmapped it would fall to `most_significant_event_type`'s -1 and rank beneath
    # every other type — a director attachment would lose to anything it shared a day with.
    "crew_attached": "announced",
    "production_start": "shooting",
    "production_wrap": "wrapped",
    "release_date": "dated",
    "trailer": "trailer",
}


def derive_arc_stage(status: str | None) -> str:
    """Return a film's current arc stage, derived solely from its TMDB status.

    News events never influence production status (NEU-452): a low-trust or fabricated
    beat must not shift a film's stage. Only `film.status` (from TMDB) maps to a stage.
    """
    return ARC_STAGES[_RANK[_STATUS_BASELINE.get(status or "", "announced")]]


def most_significant_event_type(event_types: Iterable[str]) -> str:
    """Return the most-significant event_type from a non-empty group, using the same
    ordering as the film arc. event_types with no arc stage (i.e. "other") rank below
    "announced", so an other-only group returns "other"."""
    return max(
        event_types,
        key=lambda t: _RANK[_EVENT_STAGE[t]] if t in _EVENT_STAGE else -1,
    )


def ordered_event_types(event_types: Iterable[str]) -> list[str]:
    """Return the distinct event_types of a group, most-significant first.

    Same ordering as `most_significant_event_type` — whose result is this list's head — so a
    caller can render the whole set of a day's beats without the lead one moving. Types
    sharing a rank (`first_look` and `other` both fall to -1) are ordered alphabetically:
    rank alone would leave them in whatever order `array_agg` returned, which is not stable
    between requests.
    """
    return sorted(
        set(event_types),
        key=lambda t: (-(_RANK[_EVENT_STAGE[t]] if t in _EVENT_STAGE else -1), t),
    )
