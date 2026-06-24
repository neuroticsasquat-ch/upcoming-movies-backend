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
    "production_start": "shooting",
    "production_wrap": "wrapped",
    "release_date": "dated",
    "trailer": "trailer",
}


def derive_arc_stage(status: str | None, event_types: Iterable[str]) -> str:
    """Return a film's current arc stage."""
    best = _RANK[_STATUS_BASELINE.get(status or "", "announced")]
    for event_type in event_types:
        stage = _EVENT_STAGE.get(event_type)
        if stage is not None:
            best = max(best, _RANK[stage])
    return ARC_STAGES[best]


def most_significant_event_type(event_types: Iterable[str]) -> str:
    """Return the most-significant event_type from a non-empty group, using the same
    ordering as the film arc. event_types with no arc stage (i.e. "other") rank below
    "announced", so an other-only group returns "other"."""
    return max(
        event_types,
        key=lambda t: _RANK[_EVENT_STAGE[t]] if t in _EVENT_STAGE else -1,
    )
