from collections.abc import Iterable

ARC_STAGES: tuple[str, ...] = (
    "announced",
    "cast",
    "shooting",
    "wrapped",
    "dated",
    "trailer",
    "released",
    # Terminal, and above `released` deliberately (EF-6, NEU-1435). Nothing a film does after
    # being called off outranks having been called off, so a day that carries the cancellation
    # and anything else reads as the cancellation.
    #
    # **No film is ever *in* this stage.** `_STATUS_BASELINE` has no `Canceled` entry and does
    # not gain one here: `arc_stage` is an API field the frontend renders from a closed
    # vocabulary, and widening what `derive_arc_stage` can return is a contract change this
    # ticket has no mandate for. The stage exists so `_EVENT_STAGE` has a rank to point at —
    # `event_stage_rank` is the whole of what it is for.
    "canceled",
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
    # A studio attaching is the same "this is real now" beat a director attaching is (EF-5):
    # a film with a distributor behind it has moved, and on a day that carries nothing else
    # that is what the group should read as. `company_removed` is deliberately absent, like
    # `credit_removed` — a detachment is a correction to an arc, not a stage of one, and at
    # -1 it yields to any real beat it shares a day with.
    "company_attached": "announced",
    # A film filed under a franchise is the same "this is real now" beat (EF-5, NEU-1434):
    # TMDB files a title under a collection once the sequel it belongs to is a real thing, so
    # on a day that carries nothing else that is what the group should read as.
    # `collection_removed` is absent on `company_removed`'s reasoning.
    "collection_attached": "announced",
    # A film being called off is terminal and outranks every other beat it shares a day with
    # (EF-6, NEU-1435) — if TMDB cancels a film on the day its trailer card landed, the group
    # is about the cancellation. See `ARC_STAGES` for why no film's `arc_stage` is ever this.
    "canceled": "canceled",
    "production_start": "shooting",
    "production_wrap": "wrapped",
    "release_date": "dated",
    "trailer": "trailer",
    # The film is watchable at home (D-28) — the last beat its arc has, and above the
    # `release_date` card that announced the date. On a day that carries both, availability is
    # the thing a reader can act on, so it leads.
    "now_available": "released",
}


def derive_arc_stage(status: str | None) -> str:
    """Return a film's current arc stage, derived solely from its TMDB status.

    News events never influence production status (NEU-452): a low-trust or fabricated
    beat must not shift a film's stage. Only `film.status` (from TMDB) maps to a stage.
    """
    return ARC_STAGES[_RANK[_STATUS_BASELINE.get(status or "", "announced")]]


def event_stage_rank(event_type: str) -> int:
    """Where one event type sits on the arc — the ranking `most_significant_event_type` and
    `ordered_event_types` both sort by, exposed so a caller can order *rows* by the same
    scale rather than reimplementing it.

    A type with no arc stage (`other`, `first_look`) ranks below `announced` at -1, which is
    what keeps an other-only group from outranking a real beat.
    """
    return _RANK[_EVENT_STAGE[event_type]] if event_type in _EVENT_STAGE else -1


def most_significant_event_type(event_types: Iterable[str]) -> str:
    """Return the most-significant event_type from a non-empty group, using the same
    ordering as the film arc. event_types with no arc stage (i.e. "other") rank below
    "announced", so an other-only group returns "other"."""
    return max(event_types, key=event_stage_rank)


def ordered_event_types(event_types: Iterable[str]) -> list[str]:
    """Return the distinct event_types of a group, most-significant first.

    Same ordering as `most_significant_event_type` — whose result is this list's head — so a
    caller can render the whole set of a day's beats without the lead one moving. Types
    sharing a rank (`first_look` and `other` both fall to -1) are ordered alphabetically:
    rank alone would leave them in whatever order `array_agg` returned, which is not stable
    between requests.
    """
    return sorted(set(event_types), key=lambda t: (-event_stage_rank(t), t))
