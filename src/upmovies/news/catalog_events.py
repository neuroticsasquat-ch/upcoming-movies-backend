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

# The TMDB `status` values that card, and the event type each becomes. `Released` is a real
# transition with no event type in scope, and an unrecognised status is new data from upstream;
# both are dropped rather than guessed at.
#
# `Canceled` joined them in EF-6 (NEU-1435). It is the beat entity followers most want — a film
# being called off is the last thing anyone following its director or its studio needs to hear —
# and it is the one member of this mapping that is not a production milestone: the others say
# where a film has got to, this one says it has stopped. Everything else about it is the
# milestones' terms, which is why it lives here rather than in a phase of its own: `confirmed`
# (ADR-0002 makes TMDB the record for its own scalar fields, so the change *is* the
# corroboration), no quarantine (a scalar field, not a credit somebody could invent), and at most
# one per film.
STATUS_EVENT_TYPES: dict[str, str] = {
    "In Production": "production_start",
    "Post Production": "production_wrap",
    "Canceled": "canceled",
}

# The event type a cancellation cards as (EF-6, NEU-1435). Named so the delivery half can reach
# it without re-deriving it from the status mapping — M3 selects it for every follower of an
# entity attached to the film, not only for its title followers (EF-3).
CANCELED_EVENT_TYPE = "canceled"

# A film enters production once, wraps once, and is called off once. For these the whole
# matching rule is "does this film already have one" — no window, no timestamp comparison, on
# either side. `canceled` takes the milestones' rule unchanged rather than one of its own: a
# film uncancelled and re-cancelled is TMDB correcting itself, not a second beat, and the
# reversal test in the sweep pins that.
ONCE_PER_FILM_EVENT_TYPES = frozenset(STATUS_EVENT_TYPES.values())

# The event type each *recorded* credit role cards as (spec §5.2, D-49). Director and writer
# share one type: they are one beat, and TMDB commonly gains both in a single edit — which
# `uq_event_catalog_change` would refuse as two catalog events at one timestamp anyway.
# `casting` is an existing type; `crew_attached` is new with the credit half, and has to be
# registered wherever the vocabulary is enumerated (`public.arc._EVENT_STAGE`,
# `link.cluster._STALE_EVENT_TYPES`, `ck_event_type`) or it ranks below everything.
#
# `crew` joins them at recorded grade (D-49, EF-2): a followed person's non-seed crew credit
# is a crew attachment like a director's, so it cards as `crew_attached` and groups
# with one — a cinematographer and a director attaching in the same pass are one card, which
# is what D-7's burst grouping already means by one beat. No new event type, so `ck_event_type`
# is untouched. Keyed by `catalog.seed_grade.recorded_role`, which is total, so every key this
# is subscripted with is present.
CREDIT_ROLE_EVENT_TYPES: dict[str, str] = {
    "director": "crew_attached",
    "writer": "crew_attached",
    "crew": "crew_attached",
    "cast": "casting",
}

# The event types a credit attachment can raise. Matched neither on existence nor on a window
# but on *who* — `Event.subject_key` — because a film gains cast repeatedly and the question
# is always "is this person already carded", never "is this film already carded".
CREDIT_EVENT_TYPES = frozenset(CREDIT_ROLE_EVENT_TYPES.values())

# The shared vocabulary home for the detachment carding phase.
CREDIT_REMOVED_EVENT_TYPE = "credit_removed"

# The studio half (EF-5, NEU-1433). A production company joining or leaving a film, read out
# of `catalog.film_company_change` the way the credit types are read out of
# `film_credit_change`. Registered in `ck_event_type` and in `public.arc._EVENT_STAGE`
# (`company_attached` only, for the reason `CREDIT_REMOVED_EVENT_TYPE` is absent from it: a
# detachment is a correction to an arc, not a stage of one), and deliberately **not** in
# `HIDDEN_EVENT_TYPES` — a studio attaching is a beat the timeline shows.
#
# Absent from `CATALOG_EVENT_TYPES` below, like `now_available` and unlike the credit types:
# that set is `link.cluster._catalog_dedup_target`'s, for beats the LLM reaches under another
# name, and the cluster vocabulary has no organisation types at all until EF-12 gives it one.
COMPANY_ATTACHED_EVENT_TYPE = "company_attached"
COMPANY_REMOVED_EVENT_TYPE = "company_removed"

# The pair, in the order the sweep cards them: attachments first, so a film that gained one
# studio and lost another in the same pass reads forwards.
COMPANY_EVENT_TYPES: tuple[str, ...] = (
    COMPANY_ATTACHED_EVENT_TYPE,
    COMPANY_REMOVED_EVENT_TYPE,
)

# The franchise half (EF-5, NEU-1434). A film filed under a TMDB collection, or moved out of
# one. Unlike the studio half this needed no new history table: `collection_id` is a plain
# `catalog.film` column outside `FILM_FIELD_CHANGE_DENYLIST`, so `film_field_change` has been
# recording every change to it since the trigger shipped — `ingest.sweep.collection_events`
# reads those rows, and a *move* (`id -> id'`) is the one transition that is two beats.
# Registered on exactly the studio half's terms: in `ck_event_type` and in
# `public.arc._EVENT_STAGE` (`collection_attached` only), and deliberately **not** in
# `HIDDEN_EVENT_TYPES` — a film joining a franchise is the whole of what a franchise follow
# delivers (EF-3).
#
# Absent from `CATALOG_EVENT_TYPES` below for the reason the company types are: that set is
# `link.cluster._catalog_dedup_target`'s, and the cluster vocabulary has no organisation types
# at all until EF-12 gives it one.
COLLECTION_ATTACHED_EVENT_TYPE = "collection_attached"
COLLECTION_REMOVED_EVENT_TYPE = "collection_removed"

# The pair, in the order the sweep cards them, matching `COMPANY_EVENT_TYPES`.
COLLECTION_EVENT_TYPES: tuple[str, ...] = (
    COLLECTION_ATTACHED_EVENT_TYPE,
    COLLECTION_REMOVED_EVENT_TYPE,
)

# Every event type a catalog change can raise. `release_date` is the odd one out among the
# field-change types: a film's date may move repeatedly, so it is the only one of those
# matched on *when* rather than on existence.
#
# `canceled` arrives here through `ONCE_PER_FILM_EVENT_TYPES`, and that is right rather than
# incidental: the LLM cannot emit the type today (`link.cluster._VALID_TYPES` has no member for
# it), but the day it can, a trade story reporting a cancellation belongs on the card TMDB's own
# status flip already raised — which is exactly the once-per-film rule the production milestones
# get. The organisation types are absent for the opposite reason, recorded above them.
CATALOG_EVENT_TYPES = ONCE_PER_FILM_EVENT_TYPES | {"release_date"} | CREDIT_EVENT_TYPES

# The type the watch-provider poll raises the first time a film is observed under a monetization
# type (D-28). Registered in `ck_event_type` and in `public.arc._EVENT_STAGE`, and deliberately
# absent from every set below it: `CATALOG_EVENT_TYPES` (and so
# `link.cluster._catalog_dedup_target`), `link.cluster._VALID_TYPES` and `_STALE_EVENT_TYPES`.
# The LLM has no such type in its vocabulary and cannot emit one, so there is no story-borne
# card for a poll to dedup against and no stale-stage rule to apply — unlike `crew_attached`,
# which the model reaches under another name. The poll's own ledger is the whole dedup rule.
NOW_AVAILABLE_EVENT_TYPE = "now_available"


# --- The trailer half (D-35) ---------------------------------------------------------------
#
# Unlike `now_available`, `trailer` is *not* a type the catalog path invented: the LLM has had
# it in `link.cluster._VALID_TYPES` since the story path shipped, and it is one of
# `_SINGULAR_BEAT_TYPES` — so a trade story about the same trailer joins the card this poll
# raised through that rule, with no entry here. It is deliberately kept out of
# `CATALOG_EVENT_TYPES` for the same reason: `_catalog_dedup_target` exists for beats the model
# reaches under another name or at another time, and a trailer is neither.

TRAILER_EVENT_TYPE = "trailer"

# The one hosting site whose videos card, matched case-insensitively against TMDB's `site`.
# Narrow because the beat is "there is a trailer you can watch", and the card carries a single
# key that the film page embeds as a YouTube player (NEU-1386) — a Vimeo key in that field
# would render an empty box.
TRAILER_SITE = "youtube"

# The one TMDB video `type` that is this beat. `Teaser` is deliberately excluded: it is a
# different promise to a reader, and a film that teases and then trailers would card twice for
# what the product calls one moment.
TRAILER_VIDEO_TYPE = "trailer"

_VIDEO_SUBJECT_PREFIX = f"{TRAILER_SITE}:"


def video_subject_key(key: str) -> list[str]:
    """The `Event.subject_key` a trailer card carries: one `youtube:<key>` token.

    Prefixed the way `now_available`'s `US:rent` tokens are, so a bare id can never be mistaken
    for a person's name on a casting card — `subject_key` is one column shared by every event
    type, and the prefix is what keeps the namespaces apart.
    """
    return [f"{_VIDEO_SUBJECT_PREFIX}{key}"]


def video_key_of(event_type: str, subject_key: list[str] | None) -> str | None:
    """The YouTube key a trailer card carries, for `EventOut.video_key` — or None.

    None for every event that is not a catalog-born trailer card, which is most of them: a
    story-born `trailer` event has no video behind it at all, only the outlets that reported
    one, so the film page has nothing to embed and must fall back to the card's sources.
    """
    if event_type != TRAILER_EVENT_TYPE or not subject_key:
        return None
    for token in subject_key:
        if token.startswith(_VIDEO_SUBJECT_PREFIX):
            return token[len(_VIDEO_SUBJECT_PREFIX) :]
    return None
