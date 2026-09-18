"""The Tier-A short-circuit: a trade story publishes a quarantined credit (NEU-1371, D-5).

INV-4. A credit attachment waits out `SWEEP_CREDIT_QUARANTINE_HOURS` before the sweep will
card it (ADR-0017, D-3). If a trade breaks the same casting while it waits, the story card
publishes immediately — and the quarantined change must then publish *as that card* rather
than surface days later as a second card saying the same thing. Tier-A is a trade feed story,
and every story today comes from the eight curated feeds, so every story qualifies.

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

**Matching is by normalized name**, the same `subject_key` both carding paths already write
and read, which is what lets the two directions agree about who a card is about. It is also
this module's known weakness — aliases and typos miss — and the M4 seam below is where that
is fixed.

**Type scoping is deliberately wide here.** `casting` and `crew_attached` are both searched,
in both directions, because the LLM's vocabulary has no `crew_attached`: a story about a
director attaching comes back classified `casting`, and the change it confirms carries the
`director` role. Matching on type would let exactly that pair through as a duplicate. The
sweep's own suppression (`_uncarded_credits`) keeps its narrower own-type rule — it never
sees a story-published change now, because the loader dropped it.

**M4 seam — resolved person ids.** Once `news.story_person` resolves a story's people to
`catalog.person` rows (M4), a card's resolved person ids are a better key than its subject
names, and `_confirmed_names` is the one place that would change: prefer the resolved ids
where a story has them, fall back to normalized names where it does not. Nothing else in this
module reads names, and nothing outside it decides who a card is about.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import FilmCreditChange, Person
from upmovies.catalog.queries import present_seed_credits
from upmovies.catalog.seed_grade import credit_role
from upmovies.ingest.credit_holds import open_hold_keys
from upmovies.ingest.tmdb.credit_history import CREDIT_ADDED
from upmovies.news.catalog_events import CREDIT_EVENT_TYPES
from upmovies.news.models import Event
from upmovies.news.subject_key import normalize_name

log = logging.getLogger(__name__)


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
    the seed-grade role it carries. ORM rows rather than columns, because the caller's whole
    job is to write `carded_by_event_id` back onto them.

    Rows whose `(credit_type, job)` carries no seed grade are dropped here rather than in SQL,
    for the reason `load_attachment_backlog` filters holds in Python: the role is what
    `credit_role` decides, and nothing in the row spells it.

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
        role = credit_role(change.credit_type, change.job)
        if role is None:
            continue
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

    The credit must still be in `catalog.film_credit` under the same seed-grade role. Without
    that check a story naming someone TMDB has *already reverted* would stamp the reverted
    change and mark as published something quarantine exists to make sure never publishes;
    the story card itself is untouched either way, since a trade's word does not depend on
    TMDB's. It reads the same `present_seed_credits` the quarantine gate does, so both gates
    mean one thing by "still attached".

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
    present = await present_seed_credits(session, film_ids={film_id})
    stamped = 0
    for change, _name, role in pending:
        if (change.film_id, change.person_id, role) not in present:
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
    session: AsyncSession, *, since: datetime, within_days: int
) -> int:
    """Backward direction: a pending attachment finds the story card that already published
    it. Returns how many rows it stamped.

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
    pending = await _pending_changes(session, floor=since)
    if not pending:
        return 0
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
    return stamped
