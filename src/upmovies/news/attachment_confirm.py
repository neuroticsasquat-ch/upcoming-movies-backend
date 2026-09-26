"""The Tier-A short-circuit: a trade story publishes a quarantined attachment (NEU-1371, D-5).

INV-4. An attachment waits out `SWEEP_CREDIT_QUARANTINE_HOURS` before the sweep will card it
(ADR-0017, D-3) — a credit, a production-company row and a collection assignment alike. If a
trade breaks the same beat while it waits, the story card publishes immediately — and the
quarantined change must then publish *as that card* rather than surface days later as a second
card saying the same thing. Tier-A is a trade feed story, and every story today comes from the
eight curated feeds, so every story qualifies.

**Three kinds, one module** (EF-13, D-1446.3). It began as `credit_confirm.py`, people only,
because people were the only kind a story could name. EF-12 gave the extraction pass studios
and franchises (NEU-1445), so the same short-circuit is owed to them; generalising the module
rather than mirroring it beside itself is what keeps one answer to "which card published this
change".

**The link is a stamp on the change row.** `film_credit_change.carded_by_event_id` records
which event published which change. A stamped row is published already:
`load_attachment_backlog` drops it, so the sweep never sees it and no suppression rule has to
grow a special case. Choosing the change row over a release row in `ingest.credit_hold` is
what makes the link durable and queryable — "who had it first" is answerable from the row for
as long as the history keeps it, which `ingest.credit_hold` (a log of *held* items, which a
short-circuited row never is) could not answer at all.

**Both directions stamp, because either can be first.** The sweep runs ~2h *before* the daily
chain, so a story about a credit is carded after that day's sweep pass:

- *Story after change* — the cluster stage stamps, as it creates or joins a credit card.
- *Change after story* — the sweep's loader stamps, because the trades routinely scoop TMDB
  and the credit turns up days after the card.

**People are matched by normalized name; organisations by resolved id.** The name match is
the same `subject_key` both carding paths already write and read, which is what lets the two
person directions agree about who a card is about. It is also that half's known weakness —
aliases and typos miss — and the M4 seam below is where it would be fixed. An organisation has
no name to match on and does not need one: `news.story_entity` resolves it to a TMDB id
(EF-12), and the id is exact.

**Type scoping is deliberately wide here.** `casting` and `crew_attached` are both searched,
in both directions, because the LLM's vocabulary has no `crew_attached`: a story about a
director attaching comes back classified `casting`, and the change it confirms carries the
`director` role. Matching on type would let exactly that pair through as a duplicate. The
sweep's own suppression (`_uncarded_credits`) keeps its narrower own-type rule — it never
sees a story-published change now, because the loader dropped it.

**Organisations stamp backward only** (D-1446.6). The forward direction runs from the cluster
stage, as a card is created or joined — and at that moment an organisation mention has not been
resolved yet, because the resolve stage runs later on its own backlog. So `stamp_prior_story_cards`
carries all three kinds and `stamp_story_confirmed_changes` stays person-only. Nothing is lost:
the sweep's loader runs before its carder, so a change TMDB had first is stamped on the pass
that would otherwise have carded it, and waits no longer than it was waiting anyway.

**Confirmation is the other half of the stamp** (D-1446.4). A story-formed attach card
publishes `rumored` — "in talks" — and is owed a `confirmed` upgrade when the catalog observes
the change it predicted. `confirm_stamped_cards` is that flip, in `ingest.sweep.confirm_events`
rather than here: it supersedes cards and runs as a sweep phase, both of which are the sweep's
business, and this module sits below the sweep in the import graph.

**M4 seam — resolved person ids.** Once `news.story_person` resolves a story's people to
`catalog.person` rows, a card's resolved person ids are a better key than its subject names,
and `_confirmed_names` is the one place that would change: prefer the resolved ids where a
story has them, fall back to normalized names where it does not. Nothing else in this module
reads person names, and nothing outside it decides who a card is about. Unchanged by M4, which
gave the *organisation* kinds their ids and left this half alone.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.follow_queries import followed_people
from upmovies.catalog.models import (
    COLLECTION_FIELD,
    FilmCompanyChange,
    FilmCreditChange,
    FilmFieldChange,
    Person,
)
from upmovies.catalog.queries import present_recorded_credits
from upmovies.catalog.seed_grade import recorded_credit_key, recorded_role
from upmovies.ingest.credit_holds import open_hold_keys
from upmovies.ingest.tmdb.company_history import COMPANY_ADDED
from upmovies.ingest.tmdb.credit_history import CREDIT_ADDED
from upmovies.news.catalog_events import (
    COLLECTION_CHANGE_EVENT_TYPES,
    COMPANY_ATTACHED_EVENT_TYPE,
    COMPANY_REMOVED_EVENT_TYPE,
    CREDIT_EVENT_TYPES,
    collection_field_events,
)
from upmovies.news.models import (
    ORGANISATION_KINDS,
    PERSON_KIND,
    RESOLVED_MENTION_PATHS,
    Event,
    EventStory,
    StoryEntity,
)
from upmovies.news.subject_key import normalize_name

log = logging.getLogger(__name__)

KINDS: tuple[str, ...] = (PERSON_KIND, *ORGANISATION_KINDS)
"""Every kind the backward stamp covers, and `stamp_prior_story_cards`' default.

Built from the two vocabularies rather than spelled, so a fourth kind of entity — the one
EF-12's `story_entity` was shaped to take — is registered in `news.models` and reaches this
module without a second list to remember."""

_ORGANISATION_CARD_KINDS: dict[str, str] = {
    COMPANY_ATTACHED_EVENT_TYPE: "company",
    COMPANY_REMOVED_EVENT_TYPE: "company",
    **{t: "collection" for t in COLLECTION_CHANGE_EVENT_TYPES.values()},
}
"""Which `news.story_entity.kind` each organisation card type is about.

Both halves of the join need it: the card type filters `news.event`, the kind filters the
mention, and a `collection_attached` card carrying a resolved *company* mention of the same id
is a coincidence, not a match. Keyed by card type because that is the column being read.
"""


def _confirmed_names(event: Event) -> set[str]:
    """The normalized names a credit card claims to be about, or an empty set when it is not
    a credit card at all. The M4 seam: this is where resolved `story_person.person_id` values
    would take over from names."""
    if event.event_type not in CREDIT_EVENT_TYPES:
        return set()
    return {n for n in (event.subject_key or []) if n}


async def _pending_changes(
    session: AsyncSession, *, floor: datetime, film_ids: Sequence[UUID] | None = None
) -> list[tuple[FilmCreditChange, str, str]]:
    """Unstamped `added` rows at or after `floor`, each with its person's normalized name and
    the recorded role it carries. ORM rows rather than columns, because the caller's whole
    job is to write `carded_by_event_id` back onto them.

    The role is derived in Python rather than in SQL, for the reason `load_attachment_backlog`
    filters holds there: `recorded_role` decides it, and nothing in the row spells it. Nothing
    is dropped for having no role — every row here was recorded by `credit_history`, so it has
    one by construction (D-49).

    **Rows under an open sanity hold are read past** (D-8), on the same terms and by the same
    key as `load_attachment_backlog`. A held row is one the sweep has deliberately taken out
    of the publishing path until a condition clears or an admin releases it, and a stamp is
    permanent — stamping one would convert "a human will decide about this" into "nobody ever
    will", and leave `reconcile_holds` expiring a hold over a change that could no longer card
    whatever it decided. Nothing is lost by waiting: a released row is back in this set on the
    pass that released it, and the backward direction stamps it against the same story card
    then. The story card itself publishes either way — a trade's word never waited on a hold
    of ours.
    """
    stmt = (
        select(FilmCreditChange, Person.name)
        .join(Person, Person.id == FilmCreditChange.person_id)
        .where(
            FilmCreditChange.change == CREDIT_ADDED,
            FilmCreditChange.carded_by_event_id.is_(None),
            FilmCreditChange.changed_at >= floor,
        )
        .order_by(FilmCreditChange.changed_at, FilmCreditChange.id)
    )
    if film_ids is not None:
        stmt = stmt.where(FilmCreditChange.film_id.in_(film_ids))
    held = await open_hold_keys(session, since=floor)
    pending: list[tuple[FilmCreditChange, str, str]] = []
    for change, name in await session.execute(stmt):
        role = recorded_role(change.credit_type, change.job)
        if (change.film_id, change.person_id, role, change.changed_at) in held:
            continue
        pending.append((change, normalize_name(name), role))
    return pending


async def stamp_story_confirmed_changes(
    session: AsyncSession, *, film_id: UUID, event: Event, now: datetime, within_days: int
) -> int:
    """Forward direction: a story card just created or joined publishes the film's pending
    attachments for the people it names. Returns how many rows it stamped.

    Runs inside the cluster item's transaction — the caller owns the commit, as everything in
    `apply_cluster_decisions` does — so a story and the changes its card published land
    together or not at all.

    The credit must still be in `catalog.film_credit` under the same recorded role. Without
    that check a story naming someone TMDB has *already reverted* would stamp the reverted
    change and mark as published something quarantine exists to make sure never publishes;
    the story card itself is untouched either way, since a trade's word does not depend on
    TMDB's. It reads the same `present_recorded_credits` the quarantine gate does, with the
    same followed set, so both gates mean one thing by "still attached".

    `within_days` bounds this at `changed_at >= now - within_days`: a story is confirmation of
    a *recent* attachment, and beyond the window it is a retrospective that must not retire a
    change the sweep has its own reasons for holding.
    """
    names = _confirmed_names(event)
    if not names or within_days <= 0:
        return 0
    pending = await _pending_changes(
        session, floor=now - timedelta(days=within_days), film_ids=[film_id]
    )
    pending = [(c, n, r) for c, n, r in pending if n in names]
    if not pending:
        return 0
    followed = set((await session.execute(followed_people())).scalars().all())
    present = await present_recorded_credits(session, film_ids={film_id}, followed=followed)
    stamped = 0
    for change, _name, _role in pending:
        key = (
            change.film_id,
            change.person_id,
            *recorded_credit_key(change.credit_type, change.job),
        )
        if key not in present:
            continue
        change.carded_by_event_id = event.id
        stamped += 1
    if stamped:
        log.info(
            "tier-a short-circuit: story card %s published %d pending credit change(s) on film %s",
            event.id,
            stamped,
            film_id,
        )
    return stamped


async def stamp_prior_story_cards(
    session: AsyncSession, *, since: datetime, within_days: int, kinds: tuple[str, ...] = KINDS
) -> int:
    """Backward direction: a pending change finds the story card that already published it.
    Returns how many rows it stamped.

    Every carder's loader calls this before reading its own backlog, because stamping is what
    takes a row *out* of that backlog. One function rather than three, so a change of window
    rule or of "which card counts" cannot reach one kind and miss another.

    `kinds` defaults to all three and each phase passes its own, exactly as each phase passes
    its own `fields` to `sweep.field_events.load_change_backlog` and for the same reason: three
    carders run per sweep, and a stamper that swept all three kinds from each of them would do
    the same work three times and report it three times over.

    The trades scoop TMDB often enough that this is not the rare half: the card exists first
    and the credit appears days later, by which time the cluster stage has long finished with
    that story and will never look at this film again.

    Two queries for the whole pass rather than a lookup per row — the pending set and the
    story cards covering its films — because the rolling window re-presents the same rows on
    every pass, and a per-row query would make the cost of a quiet window scale with how long
    the window is.

    No presence check, unlike the forward direction. A row reaching here has not been through
    the quarantine gate yet and does not need to be: if TMDB has since reverted the credit the
    gate would withhold it anyway, and either way the beat the trades ran is already published
    and must not be carded twice.

    `within_days` is measured from each change's own `changed_at`, not from `now`: the
    question is whether the story was reporting *this* attachment, and a card from three weeks
    before the credit landed was reporting something else. Bounded below only — a card that
    landed *after* the credit still published it, and is the ordinary shape whenever the
    forward direction missed it (the change was outside the cluster stage's own window, or
    TMDB had momentarily reverted the credit when the story clustered). `since` is the sweep's
    rolling window — rows older than it are not carded by anything, so stamping them would be
    writing history nothing reads.
    """
    if within_days <= 0:
        return 0
    organisations = tuple(k for k in kinds if k in ORGANISATION_KINDS)
    stamped_organisations = (
        await _stamp_prior_organisation_cards(
            session, since=since, within_days=within_days, kinds=organisations
        )
        if organisations
        else 0
    )
    if PERSON_KIND not in kinds:
        return stamped_organisations
    pending = await _pending_changes(session, floor=since)
    if not pending:
        return stamped_organisations
    cards = (
        await session.execute(
            select(Event.id, Event.film_id, Event.subject_key, Event.occurred_at).where(
                Event.film_id.in_({c.film_id for c, _, _ in pending}),
                Event.provenance == "story",
                Event.event_type.in_(CREDIT_EVENT_TYPES),
                Event.subject_key.isnot(None),
            )
        )
    ).all()
    by_film: dict[UUID, list[tuple[UUID, set[str], datetime]]] = {}
    for card_id, card_film_id, subject_key, occurred_at in cards:
        by_film.setdefault(card_film_id, []).append(
            (card_id, {n for n in (subject_key or []) if n}, occurred_at)
        )
    # Oldest first, so the match below takes the *earliest* qualifying card naming the person.
    # With two story cards about one person on one film, the earlier one is the scoop and the
    # later one is the rest of the trades repeating it — and it is the scoop this row has to
    # point at, because §5 reads `carded_by_event_id`'s card as the answer to who had it
    # first. Stamping the corroboration would make a beat the trades broke on Monday read as
    # though it were published on Thursday.
    for cards_for_film in by_film.values():
        cards_for_film.sort(key=lambda c: c[2])
    window = timedelta(days=within_days)
    stamped = 0
    for change, name, _role in pending:
        for card_id, card_names, occurred_at in by_film.get(change.film_id, ()):
            if name in card_names and occurred_at >= change.changed_at - window:
                change.carded_by_event_id = card_id
                stamped += 1
                break
    if stamped:
        log.info("tier-a short-circuit: %d pending credit change(s) were already carded", stamped)
    return stamped + stamped_organisations


async def _resolved_organisation_cards(
    session: AsyncSession, *, film_ids: set[UUID]
) -> dict[tuple[UUID, str, int, str], list[tuple[UUID, datetime]]]:
    """Every **published** story card on these films that a resolved `story_entity` mention
    names, keyed by `(film, kind, entity_id, card type)` and oldest first.

    `status = 'published'` is load-bearing rather than tidy. A stamped row is dropped from the
    carding backlog for good, so stamping one against a card that is no longer published would
    retire the change with *nothing* standing in for it — the beat would simply never be
    carded. A superseded card is reachable here now that a story can form an organisation
    attach card and a later detachment can supersede it (D-1446.5).

    The person half above does **not** carry this term, and is left as it is: D-1446.3 keeps
    the person functions' behaviour, and changing what a merged path stamps is not this
    ticket's to do. Worth closing next time that half is opened.

    The key carries the card type as well as the entity because an attach change may only ever
    be stamped by an attach card: a story reporting that Legendary has *left* a film has not
    published the row recording that it joined.

    The mention's own `event_type` must equal the card's type, which is the organisation half's
    version of the person half's deliberately wide `casting`/`crew_attached` scoping. It can be
    exact here because the organisation vocabulary is symmetric — a studio boarding cards as
    `company_attached` and is mentioned as `company_attached` (D-1446.1) — where a director
    signing on cards as `crew_attached` and is mentioned as `casting`.

    `RESOLVED_MENTION_PATHS` is the cut (D-25): an `unlinked` or `not_in_tmdb` mention names
    nobody, and an unresolved one carries `entity_id` NULL and is filtered out beside it.

    Two queries for the whole pass rather than a lookup per row, for the reason the person half
    gives: the rolling window re-presents the same rows on every pass.
    """
    if not film_ids:
        return {}
    stmt = (
        select(
            Event.id,
            Event.film_id,
            Event.event_type,
            Event.occurred_at,
            StoryEntity.kind,
            StoryEntity.entity_id,
        )
        .join(EventStory, EventStory.event_id == Event.id)
        .join(StoryEntity, StoryEntity.story_id == EventStory.story_id)
        .where(
            Event.film_id.in_(film_ids),
            Event.provenance == "story",
            Event.status == "published",
            Event.event_type.in_(_ORGANISATION_CARD_KINDS),
            StoryEntity.path.in_(RESOLVED_MENTION_PATHS),
            StoryEntity.entity_id.isnot(None),
            StoryEntity.features["event_type"].astext == Event.event_type,
        )
    )
    cards: dict[tuple[UUID, str, int, str], list[tuple[UUID, datetime]]] = {}
    for card_id, film_id, event_type, occurred_at, kind, entity_id in await session.execute(stmt):
        if _ORGANISATION_CARD_KINDS[event_type] != kind:
            continue
        cards.setdefault((film_id, kind, entity_id, event_type), []).append((card_id, occurred_at))
    # Oldest first, so the match below takes the *earliest* qualifying card — the scoop rather
    # than the corroboration, for the reason the person half spells out above.
    for candidates in cards.values():
        candidates.sort(key=lambda c: c[1])
    return cards


async def _stamp_prior_organisation_cards(
    session: AsyncSession, *, since: datetime, within_days: int, kinds: tuple[str, ...]
) -> int:
    """The studio and franchise half of the backward stamp (D-1446.6).

    Both kinds in one pass because they ask one question of one card table; what differs is
    only which change table carries the pending row, and how a row's *direction* is read off
    it. `kinds` is still per-kind, so the studio phase and the franchise phase each stamp their
    own rows.

    No presence check and no hold check, on the person half's terms: a row reaching here has
    not been through the quarantine gate yet, and the organisation carders hold nothing
    durably — the company burst check (D-8's shape) withholds within a pass and leaves no row.
    """
    pending = await _pending_organisation_changes(session, since=since, kinds=kinds)
    if not pending:
        return 0
    cards = await _resolved_organisation_cards(
        session, film_ids={row.film_id for row, _, _, _ in pending}
    )
    window = timedelta(days=within_days)
    stamped = 0
    for row, kind, entity_id, card_type in pending:
        for card_id, occurred_at in cards.get((row.film_id, kind, entity_id, card_type), ()):
            if occurred_at >= row.changed_at - window:
                row.carded_by_event_id = card_id
                stamped += 1
                break
    if stamped:
        log.info(
            "tier-a short-circuit: %d pending organisation change(s) were already carded", stamped
        )
    return stamped


async def _pending_organisation_changes(
    session: AsyncSession, *, since: datetime, kinds: tuple[str, ...]
) -> list[tuple[FilmCompanyChange | FilmFieldChange, str, int, str]]:
    """Unstamped organisation changes at or after `since`, each as
    `(row, kind, entity_id, the card type that would have published it)`.

    ORM rows rather than columns, because the caller's whole job is to write
    `carded_by_event_id` back onto them.

    **A collection *move* is skipped.** `film_field_change` records the `collection_id` column,
    so `id -> id'` is one row carrying two beats (`collection_field_events`) and one stamp
    column between them. Stamping it for the arrival would take the row out of the sweep's
    backlog and suppress the departure card as well — a franchise a film really did leave,
    silently unreported. Left unstamped, the sweep cards both halves and a story about either
    joins the catalog card by ADR-0014 promotion, which is the outcome the stamp exists to
    produce anyway. Pure arrivals and pure departures are the shape D-1446.6 names and the
    shape a trade scoop actually takes.
    """
    pending: list[tuple[FilmCompanyChange | FilmFieldChange, str, int, str]] = []
    companies = (
        ()
        if "company" not in kinds
        else await session.execute(
            select(FilmCompanyChange)
            .where(
                FilmCompanyChange.carded_by_event_id.is_(None),
                FilmCompanyChange.changed_at >= since,
            )
            .order_by(FilmCompanyChange.changed_at, FilmCompanyChange.id)
        )
    )
    for (row,) in companies:
        card_type = (
            COMPANY_ATTACHED_EVENT_TYPE
            if row.change == COMPANY_ADDED
            else COMPANY_REMOVED_EVENT_TYPE
        )
        pending.append((row, "company", row.company_id, card_type))
    collections = (
        ()
        if "collection" not in kinds
        else await session.execute(
            select(FilmFieldChange)
            .where(
                FilmFieldChange.field == COLLECTION_FIELD,
                FilmFieldChange.carded_by_event_id.is_(None),
                FilmFieldChange.changed_at >= since,
            )
            .order_by(FilmFieldChange.changed_at, FilmFieldChange.id)
        )
    )
    for (row,) in collections:
        beats = collection_field_events(row.old_value, row.new_value)
        if len(beats) != 1:
            continue
        direction, collection_id = beats[0]
        pending.append((row, "collection", collection_id, COLLECTION_CHANGE_EVENT_TYPES[direction]))
    return pending
