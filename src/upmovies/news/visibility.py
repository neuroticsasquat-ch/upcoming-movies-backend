"""Which events reach users — by type, and by the market a release date belongs to.

This lives in `news/` rather than `public/` because it is a fact about events, not about the
read API: the public read path, the synthesize *write* path (NEU-969) and the M7 notify pass
(NEU-1379) all depend on it. Keeping it here stops "which events are hidden" from becoming an
implicit contract between them — add a type to `HIDDEN_EVENT_TYPES`, or change what counts as a
visible market, and every side follows.

That last point is why `region_visible` moved here from `public.service` rather than being
re-derived: a decision pass that queued a digest line about a release date no surface will show
it on would be mailing news the product denies (NEU-1379).
"""

from sqlalchemy import ColumnElement, any_, or_

from upmovies.catalog.models import Film
from upmovies.catalog.release_grade import PRIMARY_REGION
from upmovies.news.models import Event

# `other` is the uncategorized catch-all where residual hype lands (NEU-367). Hidden from
# users but kept in the table, so hiding stays reversible — note that events hidden at
# creation are never summarized, so un-hiding a type needs a synthesis backfill.
#
# `company_attached` and `company_removed` are deliberately *not* here (EF-5, NEU-1433), nor
# are `collection_attached` and `collection_removed` (NEU-1434). A studio or a franchise
# joining or leaving a film is the whole of what that follow delivers (EF-3), so hiding it
# would leave the follow with nothing to show; the types are listed here only to record that
# the question was asked and answered.
#
# `canceled` is not here either (EF-6, NEU-1435), and least of all: it is the one beat every
# follow type carries — the film's own followers and every follower of an entity attached to
# it — so hiding it would hide the thing the project exists to deliver.
HIDDEN_EVENT_TYPES = ("other",)


def visible_events() -> ColumnElement[bool]:
    """SQL predicate: an event is user-facing unless its type is hidden."""
    return Event.event_type.notin_(HIDDEN_EVENT_TYPES)


def region_visible() -> ColumnElement[bool]:
    """SQL predicate: a release_date event reaches a user only when its region is global (NULL)
    or in the film's primary set — US plus the film's origin countries. Other event types are
    never region-filtered. Requires Film to be present in the query (NEU-446)."""
    return or_(
        Event.event_type != "release_date",
        Event.region.is_(None),
        Event.region == PRIMARY_REGION,
        Event.region == any_(Film.origin_country),
    )


def feed_visible() -> tuple[ColumnElement[bool], ...]:
    """The three terms every surface that *lists cards* applies: a film with a URL, a type that
    is not hidden, and a release-date region this film's readers are in.

    One tuple rather than three terms restated per surface. The flat feed, the follows page's
    `last_activity_at` and the entity pages' `/events` lists (EF-15, EF-18) all have to agree on
    what "a card a user can see" means — a page that counted a card the feed hides would date a
    follow's last activity to something the user could never find.

    The caller joins `catalog.film` and `news.event_summary` itself: the slug term and
    `region_visible` need `Film` in the query (NEU-446), and the summary join is what makes
    `EventOut.summary` non-null. Spelling the joins here would mean owning the caller's FROM.
    """
    return (Film.slug.is_not(None), visible_events(), region_visible())
