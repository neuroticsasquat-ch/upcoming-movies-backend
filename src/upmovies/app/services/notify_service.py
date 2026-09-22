"""The decision pass: who is owed what about the events published since last time (D-31).

`python -m upmovies.pipeline_run notify` runs this on its own Coolify slot, after the daily
chain. It is the only writer of `app.notification` — ingest never calls it, and no route does
either — because the decision is a fan-out over *every* user, and a route only ever knows about
the one who made the request.

**Two branches, one clause (EF-3, EF-7).** Both read the same two builders `/me/timeline`
hands to the feed — `title_follow_film_ids` and `entity_attachment_event_ids` — OR-ed exactly
as the timeline OR-s them, which is what keeps "in my digest" and "on my timeline" from
drifting into two answers. The digest is that set; the alert branch is that set cut by EF-7's
per-reach push sets and EF-8's confirmation rule. A card in both earns both rows: an alert and
a digest line are different deliveries of the same news, not duplicates of one
(`app.models.Notification`). An **unfollowed** film earns neither, and there is nothing
else that subtracts: the mute went with the watchlist it corrected (EF-14), so this pass reads
exactly what the follow builders select and needs no rule of its own.

**The push rule is a function of the reach, not of the beat** (EF-7). D-32's one closed list
was a property of the event; two follows can reach the same card for different reasons and be
owed different things by it, so the alert branch asks `follow_reach` for the two halves of the
clause as separate booleans and decides per half:

- reached by a **title** follow → `TITLE_PUSH_TYPES`, with two narrowings: `now_available`
  still answers to `user_settings.alert_stores` (D-44), and the three credit beats push only
  when the card names a **seed-grade** role on that film (EF-9 — a 12th-billed addition is
  digest-only).
- reached by an **entity** follow → `ENTITY_PUSH_TYPES`. No seed-grade cut: you followed that
  person, studio or franchise, and `entity_attachment_event_ids` has already established that
  this card names them (or is the `canceled` card of a film they are attached to).

A user who holds both follows on one card passes on whichever arm admits it, and still earns
exactly one row per `(user, event, channel)` — the decisions are ids, de-duplicated by the
unique key rather than by the branch.

**Confirmation means two different things** (EF-8, EF-10, EF-11). Everything outside the
attach and detach types waits for `confidence = 'confirmed'` as before. Those types do not,
because every *catalog* attachment card is `rumored` on its face — any TMDB editor can add a
credit — and is published only after D-3's quarantine window, which is the confirmation. So a
`provenance = 'catalog'` attach or detach card counts as confirmed by construction, and the
card that waits is the story-backed `rumored` one: "in talks", "circling". A confirmed trade
story pushes immediately (EF-11).

**A confidence flip has to reopen the window** (EF-10). When the rumored story card is later
upgraded in place — a confirmed story clustering onto it (D-6), or D-5's stamp — the upgrade is
what queues the push, and the card's `created_at` is long past. So the alert branch alone reads
a window that reopens on `updated_at`, narrowly. Nothing writes that flip yet; the arm and its
terms are in `deliverable_events`, which says what is and is not true about it.

**Push is a second channel on the alert branch, not a third branch** (D-36). A user with a
`push_subscription` row gets the same whitelisted events queued twice, `channel = 'email'` and
`channel = 'push'`, which is what `channel` is doing in the unique key. Same whitelist, same
window, same suppression — the push rows are derived from the alert branch's own event list
rather than selected again, so the two cannot come to different answers about what is worth
interrupting somebody for. The digest has no push half: it is a long read of everything a
follow reached, and that is a mail.

**Suppression is a row, not an absence** — and this is the checkpoint most easily missed
(D-39). The pass decides for unverified (D-31) and unentitled (D-37) users exactly as it does
for anyone else, then writes `status = suppressed` instead of `queued`. Skipping them silently
would make "we decided not to mail you" indistinguishable from "nobody ever considered you",
which is the question a support ticket actually asks. Both filters are applied where the
*users* are selected, in SQL, through the named rules — `entitled_user_clause()` and
`verified_user_clause()` — rather than re-derived here.

**Idempotent by the unique key, not by care.** The window is `created_at` since the last
*successful* notify run, so a failed run's window is re-read in full, and a run whose window
overlaps a crash reconsiders events it already decided.
`uq_notification_user_event_kind_channel` turns that second decision into a conflict to ignore.
Every count this pass reports is therefore taken from what the insert actually wrote, not from
what it offered.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, Text, and_, any_, cast, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.follow_queries import follow_reach, follow_scope
from upmovies.app.models import (
    DEFAULT_ALERT_STORES,
    Follow,
    Notification,
    PushSubscription,
    User,
    UserSettings,
)
from upmovies.app.verification import verified_user_clause
from upmovies.catalog.models import Film, FilmCredit, FilmCreditChange, Person
from upmovies.catalog.queries import seed_grade_credit_clause
from upmovies.catalog.seed_grade import DIRECTOR_JOB, WRITER_JOBS
from upmovies.ingest.runs import last_successful_run_started_at, record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.news.catalog_events import (
    CANCELED_EVENT_TYPE,
    COLLECTION_EVENT_TYPES,
    COMPANY_EVENT_TYPES,
    CREDIT_REMOVED_EVENT_TYPE,
    PERSON_ATTACHMENT_EVENT_TYPES,
)
from upmovies.news.models import Event, EventSummary
from upmovies.news.subject_key import sql_normalized_name
from upmovies.news.visibility import region_visible, visible_events

log = logging.getLogger(__name__)

NOTIFY_RUN_KIND = "notify"
"""This pass's own `ingest_run.kind` — the watermark is read from the last run that carried it,
so the name is part of the contract rather than a label."""

ALWAYS_ON_ALERT_TYPES = frozenset({"release_date", "trailer"})
"""The title-follow beats no setting can switch off (D-32, beside `UserSettings.alert_stores`).
A date assigned or moved and a new trailer are *why* the film is on the list."""

NOW_AVAILABLE_EVENT_TYPE = "now_available"

EMAIL_CHANNEL = "email"
PUSH_CHANNEL = "push"

Decision = tuple[UUID, str, str]
"""One row this pass may write: `(event_id, kind, channel)` — the unique key minus the user,
which every decision in a batch shares."""

ATTACHMENT_PUSH_TYPES: tuple[str, ...] = tuple(
    sorted({*PERSON_ATTACHMENT_EVENT_TYPES, *COMPANY_EVENT_TYPES, *COLLECTION_EVENT_TYPES})
)
"""Every card that says an entity joined or left a film — the types EF-8's provenance rule and
the widened alert window both key on.

`canceled` is deliberately **not** here although both push sets carry it: a cancellation is a
film-status beat that happens to reach entity followers, it is `confirmed` when it is carded,
and nothing ever upgrades one in place. Sorted, because it is rendered into an `IN`."""

SEED_GRADE_TITLE_TYPES = frozenset(PERSON_ATTACHMENT_EVENT_TYPES)
"""The three credit beats EF-9 puts a seed-grade floor under, *on the title-follow arm only*.

The floor is about proportion, not confidence: a film's follower asked about the film, and the
12th-billed addition TMDB recorded overnight is not worth a lock screen. The same card reaching
that performer's own follower is exactly what they asked for, so the entity arm has no such
cut."""

TITLE_PUSH_TYPES = frozenset(
    {
        *ALWAYS_ON_ALERT_TYPES,
        NOW_AVAILABLE_EVENT_TYPE,
        CANCELED_EVENT_TYPE,
        *SEED_GRADE_TITLE_TYPES,
    }
)
"""What may interrupt somebody about a film they follow **by title** (EF-7).

D-32's closed list plus the two things the cutover added: the film being called off, and the
credit beats — the latter under `SEED_GRADE_TITLE_TYPES`' floor. No studio or franchise type:
which company financed a film you follow is timeline news, not an interrupt."""

ENTITY_PUSH_TYPES = frozenset({*ATTACHMENT_PUSH_TYPES, CANCELED_EVENT_TYPE})
"""What may interrupt somebody about a person, studio or franchise they follow (EF-7).

The attachment stream itself and the cancellation of a film the entity is on — which is the
whole of what an entity follow delivers (`entity_attachment_event_ids`), so this set is that
builder's vocabulary and not a narrowing of it. Nothing about the film's own life reaches here:
a release date, a trailer or a streaming debut is the *film's* news and belongs to whoever
followed the film (EF-3)."""

PUSH_TYPES: tuple[str, ...] = tuple(sorted(TITLE_PUSH_TYPES | ENTITY_PUSH_TYPES))
"""The union, for the one `IN` that cuts the alert query down before Python decides per reach.
A type outside it can never push by either arm, so asking the database first is what keeps the
Python filter over a handful of rows rather than over the window."""

ALERT_STORE_BY_MONETIZATION = {"flatrate": "stream", "rent": "rent", "buy": "buy"}
"""The one place `catalog.MONETIZATION_TYPES` and `app.models.ALERT_STORES` meet.

They disagree on one word: TMDB calls a subscription offer `flatrate` (and so does
`availability_first_seen`, and so do the `US:flatrate` tokens `now_available` writes into
`subject_key`), while D-44 spells the user-facing setting `stream`. Mapping them here, once,
is what stops a `{stream}` setting — the column default, and so the value most users will
have — from silently matching nothing at all. `tests/unit/app/test_notify_decisions.py`
pins the mapping to `MONETIZATION_TYPES`, so a fourth offer kind fails a test rather than
quietly alerting nobody about itself."""


@dataclass
class NotifyResult:
    """What one decision pass considered and wrote."""

    users_considered: int = 0
    """Users holding a follow — the pass's working set, not the user table.
    Reported because `0 queued` is a perfectly healthy quiet day and says nothing on its own
    about whether the pass had anybody to decide for."""
    events_considered: int = 0
    alerts_queued: int = 0
    digests_queued: int = 0
    push_alerts_queued: int = 0
    """`channel = 'push'` alert rows, for the subset of users who have registered a browser
    (D-36). Counted apart from `alerts_queued` rather than folded into it because the two are
    different deliveries with different failure modes — a mail that bounces and a push service
    that has forgotten the endpoint — and an operator reading a run needs to see which half
    moved."""
    suppressed: int = 0
    """Rows written for a user who may not be mailed — unverified (D-31) or unentitled (D-39).
    Counted apart from the queued kinds because a number that climbs here while the others stay
    flat is the access gate working, not the pass failing."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None
    cold_start: bool = False
    """No notify run has ever succeeded, so this one only established the watermark. See
    `run_notify_pass`."""


@dataclass(frozen=True)
class Recipient:
    """One user the pass has something to decide about, and whether they may be mailed."""

    user_id: UUID
    deliverable: bool
    """Verified *and* entitled. The two suppress by the same rule and in the same place (D-39),
    so they are one answer here rather than two flags a caller could combine differently."""
    alert_stores: tuple[str, ...] = DEFAULT_ALERT_STORES
    """Which availability beats this user is alerted on (D-44).

    Read with the recipient rather than per event, because it is one row per user and the
    alternative is joining `app.user_settings` into the alert query — where a user with no
    settings row would need the same `COALESCE` all over again. `()` is a real value and means
    no store alerts; the D-32 whitelist beats are unaffected either way."""
    has_push: bool = False
    """Whether this user has a `push_subscription` row (D-36).

    A second channel, not a second decision: the push branch queues the *same* alert events
    the email branch does, so the whitelist and the suppression rules are inherited rather than
    restated. It is read here, beside `deliverable`, because a user with no registered browser
    must get no `push` row at all — a queued delivery nothing can make would sit in the backlog
    being retried every night.

    Note what it deliberately does not consider: whether the grant is live. A lapsed
    subscriber keeps their subscription rows (D-40), so `has_push` stays true and the push row
    is written `suppressed` beside the email one — which is the auditable "we decided not to
    notify you" D-39 asks for, rather than a silence that looks like nobody ever looked."""

    @property
    def status(self) -> str:
        return "queued" if self.deliverable else "suppressed"


def now_available_matches_stores(
    subject_key: Sequence[str] | None, alert_stores: Sequence[str]
) -> bool:
    """Whether a `now_available` card names an availability type this user wants (D-44, D-28).

    The card's `subject_key` carries one `region:monetization` token per type the film was
    newly seen under — one card can announce rent *and* buy in a single observation — so this
    asks whether *any* of them is wanted, not whether all are.

    A token whose monetization half is unrecognised matches nothing rather than raising: this
    runs over the whole ledger on a schedule, and one malformed key must not cost every user
    their notifications for the day. An empty or absent `subject_key` is such a card with no
    types at all, and matches nothing on the same terms."""
    wanted = set(alert_stores)
    return any(
        ALERT_STORE_BY_MONETIZATION.get(token.partition(":")[2]) in wanted
        for token in subject_key or ()
    )


def deliverable_events(since: datetime, *, include_upgrades: bool = False) -> Select[tuple[UUID]]:
    """`SELECT event.id` for every event this pass may tell *anyone* about.

    Written to be used as `Event.id.in_(deliverable_events(since))`, so both branches and the
    counter share one definition of "in scope" rather than three.

    **The window.** `created_at` rather than `occurred_at`, because publication is the axis the
    product already groups and paginates on (ADR-0016) — an event carded today about a change
    TMDB recorded last week is news to the reader today, and dating the window by `occurred_at`
    would mail nobody about it. `status = 'published'` leaves out a superseded card in favour of
    the correction that replaced it.

    **`include_upgrades` reopens it on `updated_at`, for one narrow set of cards** (EF-10). The
    alert branch is the only caller that passes it, and it has to: a story-backed attachment
    card published `rumored` — "in talks" — earns no push, and the push is owed when the
    association *confirms*, which upgrades the card in place rather than writing a second one.
    Its `created_at` is by then several runs old, so a window on publication alone would mean
    the push EF-10 promises could never arrive at all.

    The reopened arm is `attachment type AND provenance = 'story' AND confidence = 'confirmed'`,
    and every term is load-bearing, because `updated_at` moves for reasons that are not
    upgrades. `news.event.updated_at` carries a `server_default` and **no `onupdate`**, so it
    moves only where something sets it — today that is `link.cluster`'s three attach paths
    (`attach`, `dedup_attach`, `catalog_attach`), each of which is a *second outlet joining an
    existing card*, which usually changes nothing about the card at all. Without the two extra
    terms that alone would reopen every catalog attachment ever published, and a user who
    followed the entity last week would be pushed about an attachment from last year the moment
    a trade restated it. Narrowed this way the arm names exactly the cards whose push
    eligibility can have just flipped from no to yes: a catalog card was eligible the day it
    published (EF-8) and a still-`rumored` story card is not eligible now.

    **What is not yet true.** Nothing in the codebase writes that flip. `link.cluster` bumps
    `updated_at` on its attach paths but never touches `Event.confidence`, and D-5's stamp
    (`news.credit_confirm`) writes `film_credit_change.carded_by_event_id` and deliberately
    leaves the card alone. So this arm is armed and currently catches nothing: the promotion
    that upgrades a `rumored` story card to `confirmed` is owed work in the link stage, not
    here. It is spelled now, and pinned by a test that performs the flip by hand, so that
    whoever lands the promotion does not also have to discover that the pass would have ignored
    it — which is the failure EF-10 describes and the one that would show up as silence.

    Re-considering an event is cheap and idempotent — `queue_decisions` writes through
    `uq_notification_user_event_kind_channel`, so a card that already alerted this user earns
    nothing the second time, which is what makes "one push per attachment" (EF-10) a property
    of the key rather than of this window's precision. The digest branch and the
    `events_considered` counter stay on publication, because a digest line is owed when the
    card appears and a second one for the same card is not news.

    **No confidence floor** (D-1437.7). It used to sit here, on the grounds that a `rumored`
    event was not digest material either — but every catalog attach and detach card is
    `rumored` until its quarantine clears, so with the floor in the shared selector an entity
    follower's digest would carry nothing their follow delivers. EF-7 is that the digest
    carries everything the timeline carries; confirmation is what a *push* waits for. The
    alert branch applies it, and only there.

    **The visibility terms are the three the feed applies**, and they are not decoration. A
    notification is a claim, made in the user's inbox, about a card they will then click
    through to — so queueing one the product will not show them is worse than queueing
    nothing. Each cuts a real case:

    - the `EventSummary` join, because an event with no summary has no copy for a sender to put
      in a mail. Hidden types are never summarized, so this and `visible_events()` overlap —
      but only the join states the sender's actual precondition.
    - `visible_events()`, so the `other` catch-all bucket (`news.visibility`) stays out.
    - `region_visible()`, so an Indian release-date change does not mail every follower
      about a date no surface will show them — D-32 is "US theatrical or home-release". This is
      the term that needs `Film` in the query.
    - `Film.slug.is_not(None)`, because a film with no slug has no page to link to.

    `correlate(None)` for the reason `app.follow_queries` gives: the queries this is dropped
    into select from `news.event` themselves, and the builder's meaning must not depend on the
    one it lands in.
    """
    published_since = Event.created_at > since
    upgraded_since = and_(
        Event.event_type.in_(ATTACHMENT_PUSH_TYPES),
        Event.provenance == "story",
        Event.confidence == "confirmed",
        Event.updated_at > since,
    )
    window = or_(published_since, upgraded_since) if include_upgrades else published_since
    return (
        select(Event.id)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .join(Film, Film.id == Event.film_id)
        .where(
            window,
            Event.status == "published",
            Film.slug.is_not(None),
            visible_events(),
            region_visible(),
        )
        .correlate(None)
    )


async def load_recipients(session: AsyncSession) -> list[Recipient]:
    """Every user with a follow, oldest account first.

    One `EXISTS`, not two: a follow is the only thing a user keeps now (M8), so it is the whole
    working set — an account that follows nothing is owed no decision by definition, and
    selecting it would buy three statements per signup on every run.

    Both the entitlement and the verification rule are read here, as columns rather than as
    filters, because this pass owes a `suppressed` row to the users they exclude (D-39) —
    filtering them out in SQL is exactly the silent skip the decision is meant to replace.

    `alert_stores` is `COALESCE`d over an **outer** join for the reason the digest pass reads
    the cadence that way: the settings row is created lazily (`app.models.UserSettings`), so
    most users have none, and an inner join would drop every one of them — while creating one
    here would turn "this subscriber opened their settings" into "this account was once
    considered" (`app.services.settings_service`)."""
    rows = await session.execute(
        select(
            User.id,
            and_(entitled_user_clause(), verified_user_clause()).label("deliverable"),
            exists().where(PushSubscription.user_id == User.id).label("has_push"),
            func.coalesce(
                UserSettings.alert_stores, cast(list(DEFAULT_ALERT_STORES), ARRAY(Text))
            ).label("alert_stores"),
        )
        .outerjoin(UserSettings, UserSettings.user_id == User.id)
        .where(exists().where(Follow.user_id == User.id))
        .order_by(User.created_at, User.id)
    )
    return [
        Recipient(
            user_id=row.id,
            deliverable=row.deliverable,
            has_push=row.has_push,
            alert_stores=tuple(row.alert_stores),
        )
        for row in rows
    ]


def names_a_seed_grade_credit() -> ColumnElement[bool]:
    """EXISTS predicate over the enclosing `news.event`: somebody this credit card names holds,
    or demonstrably held, a **seed-grade** role on this film (EF-9).

    A credit card carries names and nothing else — `subject_key` is a list of normalized names
    (`news.subject_key`), and the role it was built from is rendered into the summary prose and
    never stored. So the grade has to be read back off the catalog, by the same name match
    `app.follow_queries._names_a_followed_person` uses, and there are two places to read it:

    - `catalog.film_credit`, under `catalog.queries.seed_grade_credit_clause` — the SQL
      spelling of `seed_grade.is_seed_grade`, reused rather than re-spelled for the reason that
      function's docstring gives. This is the arm that answers an *attachment*, and the one
      that puts EF-9's floor where the spec puts it: top-5 billing is a `film_credit` column, so
      a 12th-billed addition fails here and stays digest-only.
    - `catalog.film_credit_change`, **on a `credit_removed` card only**. `film_credit` is
      delete-and-rebuilt on every ingest, so a removed credit is simply gone from it and the
      first arm would answer no for every detachment — quietly taking that type back out of
      `TITLE_PUSH_TYPES`. The history is what remembers, and it records `credit_type` and `job`
      but no billing. Scoped to detachments rather than applied to all three types because it
      is a *fallback for a credit that is gone*: on an attachment the live row is present and
      authoritative, and letting the history answer there too would pass a 12th-billed casting
      card on the strength of an unrelated writing credit the same person once held on the film.

    That asymmetry is the one thing to know about this predicate: a **director or writer**
    leaving a film you follow pushes, and a **top-billed performer** leaving it does not,
    because nothing stores the billing of a credit TMDB has deleted. It errs toward silence,
    which is the right direction for a channel whose whole cost is interrupting somebody, and
    it is the seam to widen if `film_credit_change` ever records billing — until then EF-7's
    `credit_removed` on the title arm is met for the crew grades and not for the cast one.

    `correlate(Event)` so the predicate stays a correlated EXISTS against whichever statement
    it is dropped into — the same shape, and for the same reason, as the follow builders."""
    seed_crew_jobs = tuple(sorted({DIRECTOR_JOB, *WRITER_JOBS}))
    holds = (
        select(literal(1))
        .select_from(FilmCredit)
        .join(Person, Person.id == FilmCredit.person_id)
        .where(
            FilmCredit.film_id == Event.film_id,
            sql_normalized_name(Person.name) == any_(Event.subject_key),
            seed_grade_credit_clause(),
        )
        .correlate(Event)
        .exists()
    )
    held = (
        select(literal(1))
        .select_from(FilmCreditChange)
        .join(Person, Person.id == FilmCreditChange.person_id)
        .where(
            FilmCreditChange.film_id == Event.film_id,
            sql_normalized_name(Person.name) == any_(Event.subject_key),
            FilmCreditChange.credit_type == "crew",
            FilmCreditChange.job.in_(seed_crew_jobs),
        )
        .correlate(Event)
        .exists()
    )
    return or_(holds, and_(Event.event_type == CREDIT_REMOVED_EVENT_TYPE, held))


def confirmed_enough_to_push(event_type: str, confidence: str, provenance: str) -> bool:
    """EF-8: whether this card is confirmed *for push purposes*.

    Outside the attach and detach types the answer is D-32's, unchanged: `confirmed` or
    nothing. Inside them `provenance` decides instead, because `confidence` does not mean there
    what it means elsewhere — every catalog attachment card is written `rumored` on the grounds
    that any TMDB editor can add a credit, and then published only after D-3's quarantine
    window has passed without the credit being reverted. Surviving quarantine *is* the
    confirmation, so a `catalog` card on the timeline is already the confirmed one and waiting
    for a `confidence` flip that nothing will ever write would silence the attachment stream
    entirely.

    What is left waiting is the card EF-10 means: story-backed and `rumored` — "in talks",
    "circling". Its push arrives when it is upgraded in place, which is why the alert branch
    reads a window that reopens on `updated_at` (`deliverable_events`). A story-backed
    `confirmed` attachment pushes on the spot (EF-11)."""
    if event_type in ATTACHMENT_PUSH_TYPES:
        return provenance == "catalog" or confidence == "confirmed"
    return confidence == "confirmed"


def alert_reaches(
    event_type: str,
    *,
    via_title: bool,
    via_entity: bool,
    names_seed_role: bool,
    subject_key: Sequence[str] | None,
    alert_stores: Sequence[str],
) -> bool:
    """EF-7: whether this card may interrupt this user, given *why* it reached them.

    Pure, and takes the reach as two booleans rather than a user id, because the interesting
    cases are the ones where both are true — the follower of a film *and* of its director — and
    a signature that could only carry one reach would have to pick which of them to answer as.
    The arms are tried in turn and either admits: the entity arm first, since it is the one with
    no further narrowing to apply.

    The seed-grade flag is passed in rather than computed, because answering it means reading
    the catalog (`names_a_seed_grade_credit`) and this pass asks the question of every event in
    the window for every user — so it is selected once, in SQL, beside the row."""
    if via_entity and event_type in ENTITY_PUSH_TYPES:
        return True
    if not via_title or event_type not in TITLE_PUSH_TYPES:
        return False
    if event_type == NOW_AVAILABLE_EVENT_TYPE:
        return now_available_matches_stores(subject_key, alert_stores)
    if event_type in SEED_GRADE_TITLE_TYPES:
        return names_seed_role
    return True


async def alert_event_ids(
    session: AsyncSession,
    *,
    recipient: Recipient,
    since: datetime,
) -> list[UUID]:
    """The window's events this user earns an **alert** for (EF-7, EF-8, EF-9, EF-10, EF-11).

    The same two builders the digest reads, taken apart rather than OR-ed (`follow_reach`), so
    the per-reach decision the module docstring describes has something to decide over. The OR
    is still the scope — a card neither half reaches is not selected — it is just also carried
    into the SELECT as two labelled booleans.

    What SQL does and what Python does, and why the line falls there: SQL cuts the window, the
    scope, and the type list, and answers the one question Python cannot afford to ask per row —
    whether the card names a seed-grade role, which is a join against the catalog. Python then
    applies the three rules that are a handful of comparisons over a handful of rows, and that
    read as prose rather than as a `CASE`: the per-reach sets, the `alert_stores` check (two
    arrays through a vocabulary mapping), and EF-8's confirmation rule.

    Ordered by publication, so a batch's decisions are written in the order the cards appeared
    even when the window reopened for an upgrade dated later."""
    via_title, via_entity = follow_reach(recipient.user_id)
    rows = await session.execute(
        select(
            Event.id,
            Event.event_type,
            Event.subject_key,
            Event.confidence,
            Event.provenance,
            via_title.label("via_title"),
            via_entity.label("via_entity"),
            names_a_seed_grade_credit().label("names_seed_role"),
        )
        .where(
            Event.id.in_(deliverable_events(since, include_upgrades=True)),
            Event.event_type.in_(PUSH_TYPES),
            or_(via_title, via_entity),
        )
        .order_by(Event.created_at, Event.id)
    )
    return [
        row.id
        for row in rows
        if confirmed_enough_to_push(row.event_type, row.confidence, row.provenance)
        and alert_reaches(
            row.event_type,
            via_title=row.via_title,
            via_entity=row.via_entity,
            names_seed_role=row.names_seed_role,
            subject_key=row.subject_key,
            alert_stores=recipient.alert_stores,
        )
    ]


async def digest_event_ids(
    session: AsyncSession,
    *,
    user_id: UUID,
    since: datetime,
) -> list[UUID]:
    """The window's events this user's follows deliver — their timeline, restricted to what is
    new (EF-3, EF-7, D-33).

    `follow_scope` and nothing else: a digest that quietly covered less than the timeline it
    summarises would be exactly the drift `app.follow_queries` exists to prevent.

    No event type and no confidence is excluded here. Everything the timeline shows is digest
    material, including the whitelist beats and including a `rumored` attachment: a user who
    follows the director *and* the film is owed the alert now and the line in their weekly
    slate, which is what the unique key's `kind` column is for."""
    rows = await session.execute(
        select(Event.id)
        .where(
            Event.id.in_(deliverable_events(since)),
            follow_scope(user_id),
        )
        .order_by(Event.created_at, Event.id)
    )
    return list(rows.scalars().all())


async def queue_decisions(
    session: AsyncSession, *, recipient: Recipient, decisions: Sequence[Decision]
) -> list[Decision]:
    """Write one row per `(event_id, kind, channel)` and return the decisions actually written.

    `ON CONFLICT DO NOTHING` against `uq_notification_user_event_kind_channel` is what makes a
    re-run free, and `RETURNING` is what makes the counts honest: a second pass over the same
    window reports nothing queued because nothing was, rather than because the pass declined to
    look.

    The channel is carried on each decision rather than fixed here (D-36): the same alert is
    owed by mail and, for a user with a registered browser, by push — different deliveries of
    one piece of news, which is what `channel` is doing in the unique key at all. The
    `event_id` is returned alongside so a caller could group by event; the counters only read
    the kind and the channel."""
    if not decisions:
        return []
    rows = await session.execute(
        insert(Notification)
        .values(
            [
                {
                    "user_id": recipient.user_id,
                    "event_id": event_id,
                    "kind": kind,
                    "channel": channel,
                    "status": recipient.status,
                }
                for event_id, kind, channel in decisions
            ]
        )
        .on_conflict_do_nothing(index_elements=["user_id", "event_id", "kind", "channel"])
        .returning(Notification.event_id, Notification.kind, Notification.channel)
    )
    return [(row.event_id, row.kind, row.channel) for row in rows]


async def decide_for_user(
    session: AsyncSession,
    *,
    recipient: Recipient,
    since: datetime,
) -> list[Decision]:
    """Both branches for one user, written in one statement. Returns the decisions written.

    The alert branch produces two rows per event for a user with a registered browser (D-36):
    the same event, the same `alert` kind, once per channel. Deliberately derived from the one
    list rather than queried twice — since EF-7 "what may push" is a per-reach decision rather
    than a list, and a push branch that selected its own events would be free to come to a
    different answer than the mail that accompanies it."""
    alerts = await alert_event_ids(session, recipient=recipient, since=since)
    digests = await digest_event_ids(session, user_id=recipient.user_id, since=since)
    decisions: list[Decision] = [(event_id, "alert", EMAIL_CHANNEL) for event_id in alerts]
    if recipient.has_push:
        decisions += [(event_id, "alert", PUSH_CHANNEL) for event_id in alerts]
    # No digest by push, by design: the digest is a long read of everything a follow reached,
    # which is a mail. A notification is one beat on a lock screen.
    decisions += [(event_id, "digest", EMAIL_CHANNEL) for event_id in digests]
    return await queue_decisions(session, recipient=recipient, decisions=decisions)


async def run_notify_pass(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    failure_threshold: int = 10,
) -> NotifyResult:
    """Decide what every user is owed about the events published since the last successful run.

    **The first run ever mails nobody.** With no successful predecessor there is no watermark,
    and "everything since the beginning of time" is the whole ledger — a first deploy that
    queued an alert for every release date the catalogue has ever recorded. So a cold start
    establishes the watermark and queues nothing, deliberately losing the events published
    between the deploy and this run rather than sending a year of news at once. It reports
    `succeeded`, because it did the only sane thing available to it.

    Contract with the other passes: one session per user so a failure never rolls back the
    others, `record_progress` against the run id, abort after N consecutive failures, and **no**
    `finalize_run` — the status, error and detail line belong to whoever opened the run.
    """
    result = NotifyResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        since = await last_successful_run_started_at(s, NOTIFY_RUN_KIND)
    if since is None:
        result.cold_start = True
        log.warning(
            "notify: no successful previous run — establishing the watermark, queueing nothing"
        )
        return result

    async with owned_session(session_factory) as s:
        result.events_considered = (
            await s.execute(select(func.count()).select_from(deliverable_events(since).subquery()))
        ).scalar_one()
        recipients = await load_recipients(s)
    result.users_considered = len(recipients)
    log.info(
        "notify: %d events published since %s, %d users to decide for",
        result.events_considered,
        since.isoformat(),
        result.users_considered,
    )

    for recipient in recipients:
        await heartbeat.tick()
        try:
            async with owned_session(session_factory) as s:
                written = await decide_for_user(s, recipient=recipient, since=since)
                if written:
                    # One unit of work is one user, as it is for every other per-item loop in
                    # the pipelines; the row counts are this pass's own counters and reach
                    # `/admin/runs` through the detail line.
                    await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except Exception:
            # One user's decision must not cost the rest of the pass.
            log.exception("deciding notifications for user %s failed", recipient.user_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
                log.error("notify: %s", result.abort_error)
                return result
            continue
        guard.succeeded()
        if recipient.deliverable:
            for _, kind, channel in written:
                if kind == "digest":
                    result.digests_queued += 1
                elif channel == PUSH_CHANNEL:
                    result.push_alerts_queued += 1
                else:
                    result.alerts_queued += 1
        else:
            result.suppressed += len(written)

    log.info(
        "notify: %d alerts, %d push, %d digests, %d suppressed, %d failed",
        result.alerts_queued,
        result.push_alerts_queued,
        result.digests_queued,
        result.suppressed,
        result.failures,
    )
    return result


def notify_detail(result: NotifyResult) -> str:
    """The run's `ingest_run.detail` line.

    `suppressed` sits beside the queued kinds rather than folded into a total, for the reason
    the field itself gives: it is the access gate's own counter, and an operator reading a run
    that queued nothing needs to know at a glance whether nobody was owed anything or nobody
    was allowed anything."""
    if result.cold_start:
        return "notify: cold start — watermark established, nothing queued"
    line = (
        f"notify: {result.events_considered} events, {result.users_considered} users, "
        f"{result.alerts_queued} alerts, {result.push_alerts_queued} push, "
        f"{result.digests_queued} digests, "
        f"{result.suppressed} suppressed, {result.failures} failed"
    )
    if result.aborted:
        line += f"; notify aborted: {result.abort_error}"
    return line
