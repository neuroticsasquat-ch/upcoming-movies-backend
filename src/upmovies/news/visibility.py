"""Which event types reach users.

This lives in `news/` rather than `public/` because it is a fact about events, not about the
read API: both the public read path and the synthesize *write* path depend on it (NEU-969).
Keeping it here stops "which types are hidden" from becoming an implicit contract between the
two — add a type to `HIDDEN_EVENT_TYPES` and both sides follow.
"""

from sqlalchemy import ColumnElement

from upmovies.news.models import Event

# `other` is the uncategorized catch-all where residual hype lands (NEU-367). Hidden from
# users but kept in the table, so hiding stays reversible — note that events hidden at
# creation are never summarized, so un-hiding a type needs a synthesis backfill.
HIDDEN_EVENT_TYPES = ("other",)


def visible_events() -> ColumnElement[bool]:
    """SQL predicate: an event is user-facing unless its type is hidden."""
    return Event.event_type.notin_(HIDDEN_EVENT_TYPES)
