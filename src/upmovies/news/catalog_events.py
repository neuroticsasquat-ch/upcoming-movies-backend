"""Which events the catalog path can raise, and from what (ADR-0014).

Two packages need this vocabulary and must never disagree about it. `ingest.sweep.field_events`
reads TMDB's change history and decides what to card; `link.cluster` has to recognise the same
events coming the other way, so a trade story joins the card a change already produced instead
of opening a second one. Nothing here touches the database or the ORM — it is the shared list,
kept out of both so a new trigger (the credit half, NEU-1082) is added in one place rather than
in two that drift.

The *matching* rules deliberately do not live here: "has this change already been carded?" and
"which card does this story belong on?" are different questions with different answers, and
each is documented at its own site.
"""

# The TMDB `status` values that are a production milestone, and the event type each becomes.
# `Released` and `Canceled` are real transitions with no event type in scope; an unrecognised
# status is new data from upstream. Both are dropped rather than guessed at.
STATUS_EVENT_TYPES: dict[str, str] = {
    "In Production": "production_start",
    "Post Production": "production_wrap",
}

# A film enters production once, and wraps once. For these the whole matching rule is "does
# this film already have one" — no window, no timestamp comparison, on either side.
ONCE_PER_FILM_EVENT_TYPES = frozenset(STATUS_EVENT_TYPES.values())

# Every event type a catalog change can raise. `release_date` is the odd one out: a film's date
# may move repeatedly, so it is the only type matched on *when* rather than on existence.
CATALOG_EVENT_TYPES = ONCE_PER_FILM_EVENT_TYPES | {"release_date"}
