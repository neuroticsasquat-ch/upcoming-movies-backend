"""Marking the attach card a detachment corrects (D-2), for the two id-keyed kinds.

A detachment does not delete the attachment it ends — nothing here is ever hidden, and the
attach card keeps its place on every surface — but it does *correct* it: once a studio has left
a film, the card saying it joined is no longer the current claim, and `Event.status` says so
with `superseded` and `superseded_by`.

**Why this is not in the sweep.** It was, twice: `ingest.sweep.company_events` and
`ingest.sweep.collection_events` each grew their own copy beside the phase that cards a
detachment, and that was right while the sweep was the only thing that could raise one. EF-13
made a *story* a second source of detachments (NEU-1446), and a story-formed card can be
confirmed in two places the sweep does not reach — the resolve stage, when a mention on a card
that was already `confirmed` resolves, and the sweep's own confirmation step, which is a
different phase from the carders. Both sit above `news` in the import graph and neither may
import the other, so the shared answer lives below both.

**The removal names the entity; the entity does not have to be on the removal.** A catalog
detach card carries `company:<id>` / `collection:<id>` tokens in its `subject_key` and the
tokens are where the ids come from. A *story* detach card carries no such token and cannot:
resolution runs after clustering, so the id is not known when `subject_key` is written. So the
caller passes the ids, and a story card names what it supersedes through its resolved
`news.story_entity` rows.

**"A card names this entity" means the same two things here as everywhere else.** The prior
attach card is found by its `company:<id>` / `collection:<id>` token — a catalog card by
construction — **or** by a resolved `story_entity` mention of the same entity, typed as the
attach beat, on one of its stories. The second mechanism is not optional now that EF-13 lets a
*story* form an attach card: such a card carries no token, so matching on tokens alone would
leave it published beside a confirmed contradiction, which is the outcome D-1446.5 rejected in
so many words. It is the same two-mechanism rule `app.follow_queries._card_names_organisation`
applies on the delivery side, and it is spelled twice for one reason — that module builds a
correlated fragment for a `UNION ALL` arm and this one runs a statement per entity.
"""

from uuid import UUID

from sqlalchemy import literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from upmovies.news.models import RESOLVED_MENTION_PATHS, Event, EventStory, StoryEntity
from upmovies.news.subject_key import collection_subject_token, company_subject_token

_TOKENS = {"company": company_subject_token, "collection": collection_subject_token}
"""The `subject_key` token builder per `story_entity.kind`. Keyed by the catalog spelling, which
is what both the mention rows and the calling phases use — never the follow graph's
`franchise` (CONTEXT.md **Franchise**)."""


def _names_entity(kind: str, entity_id: int, attach_type: str):
    """ "This card names that entity", in the two ways a card can — the token it carries, or a
    resolved mention of the entity, typed as the attach beat, on one of its stories."""
    story = aliased(EventStory)
    mention = aliased(StoryEntity)
    return or_(
        Event.subject_key.any(_TOKENS[kind](entity_id)),  # pyright: ignore[reportArgumentType]
        select(literal(1))
        .select_from(story)
        .join(mention, mention.story_id == story.story_id)
        .where(
            story.event_id == Event.id,
            mention.kind == kind,
            mention.entity_id == entity_id,
            mention.path.in_(RESOLVED_MENTION_PATHS),
            mention.features["event_type"].astext == attach_type,
        )
        .correlate(Event)
        .exists(),
    )


async def supersede_prior_entity_cards(
    session: AsyncSession, *, removal: Event, attach_type: str, kind: str, entity_ids: list[int]
) -> int:
    """Mark the attach card each of `entity_ids` was current on, `superseded_by` `removal`.

    Per entity: the most recent *published* `attach_type` card on the removal's film that
    occurred before it and names that entity. Only the most recent one — an older card the same
    entity is on was already the earlier claim, not the one this removal corrects.

    The card, not the entity, is the unit of supersession: `status` lives on the event row, so
    a card naming three studios is marked when any one of them leaves.

    Returns the number of cards marked. Caller owns the commit; `removal` must be flushed so
    its id exists for the FK.
    """
    if not entity_ids:
        return 0
    # Resolve every target before marking any. Marking inside the loop would autoflush the
    # first UPDATE ahead of the next entity's query, and a card two departing entities share
    # would then fail the `published` filter for the second — handing back an *older* card
    # that entity is on, which is not the one this removal corrects.
    targets: dict[UUID, Event] = {}
    for entity_id in entity_ids:
        stmt = (
            select(Event)
            .where(
                Event.film_id == removal.film_id,
                Event.event_type == attach_type,
                _names_entity(kind, entity_id, attach_type),
                Event.status == "published",
                Event.occurred_at < removal.occurred_at,
            )
            .order_by(Event.occurred_at.desc(), Event.created_at.desc())
            .limit(1)
        )
        card = (await session.execute(stmt)).scalar_one_or_none()
        if card is not None:
            targets[card.id] = card
    for card in targets.values():
        card.status = "superseded"
        card.superseded_by = removal.id
    marked = len(targets)
    await session.flush()
    return marked
