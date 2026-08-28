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

# The event type each seed-grade credit role cards as (spec §5.2). Director and writer share
# one type: they are one beat, and TMDB commonly gains both in a single edit — which
# `uq_event_catalog_change` would refuse as two catalog events at one timestamp anyway.
# `casting` is an existing type; `crew_attached` is new with the credit half, and has to be
# registered wherever the vocabulary is enumerated (`public.arc._EVENT_STAGE`,
# `link.cluster._STALE_EVENT_TYPES`, `ck_event_type`) or it ranks below everything.
CREDIT_ROLE_EVENT_TYPES: dict[str, str] = {
    "director": "crew_attached",
    "writer": "crew_attached",
    "cast": "casting",
}

# The event types a credit attachment can raise. Matched neither on existence nor on a window
# but on *who* — `Event.subject_key` — because a film gains cast repeatedly and the question
# is always "is this person already carded", never "is this film already carded".
CREDIT_EVENT_TYPES = frozenset(CREDIT_ROLE_EVENT_TYPES.values())

# The shared vocabulary home for the detachment carding phase.
CREDIT_REMOVED_EVENT_TYPE = "credit_removed"

# Every event type a catalog change can raise. `release_date` is the odd one out among the
# field-change types: a film's date may move repeatedly, so it is the only one of those
# matched on *when* rather than on existence.
CATALOG_EVENT_TYPES = ONCE_PER_FILM_EVENT_TYPES | {"release_date"} | CREDIT_EVENT_TYPES
