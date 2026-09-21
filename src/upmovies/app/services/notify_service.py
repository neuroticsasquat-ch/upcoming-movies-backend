"""The decision pass: who is owed what about the events published since last time (D-31).

`python -m upmovies.pipeline_run notify` runs this on its own Coolify slot, after the daily
chain. It is the only writer of `app.notification` — ingest never calls it, and no route does
either — because the decision is a fan-out over *every* user, and a route only ever knows about
the one who made the request.

**Two branches, one graph (M8).** Both read `app.follow_queries`, at its two grains: the alert
branch takes `watchlist_film_ids` — what this user's follows *cover*, minus their mutes — and
admits only D-32's whitelist; the digest branch takes the
D-11 builders `/me/timeline` hands to the feed, which is what keeps "in my digest" and "on my
timeline" from drifting into two answers. A film in both sets earns both rows: an alert and a
digest line are different deliveries of the same news, not duplicates of one
(`app.models.Notification`). A **muted** film earns neither — the exclusion is inside the
builders, so it reaches this pass without a rule of its own (D-45).

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
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Select, Text, and_, cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.follow_queries import (
    events_naming_followed_people,
    followed_film_ids,
    watchlist_film_ids,
)
from upmovies.app.models import (
    DEFAULT_ALERT_STORES,
    Follow,
    Notification,
    PushSubscription,
    User,
    UserSettings,
)
from upmovies.app.verification import verified_user_clause
from upmovies.catalog.models import Film
from upmovies.ingest.runs import last_successful_run_started_at, record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.news.models import Event, EventSummary
from upmovies.news.visibility import region_visible, visible_events

log = logging.getLogger(__name__)

NOTIFY_RUN_KIND = "notify"
"""This pass's own `ingest_run.kind` — the watermark is read from the last run that carried it,
so the name is part of the contract rather than a label."""

ALWAYS_ON_ALERT_TYPES = frozenset({"release_date", "trailer"})
"""The whitelist beats no setting can switch off (D-32, beside `UserSettings.alert_stores`).
A date assigned or moved and a new trailer are *why* the film is on the list."""

NOW_AVAILABLE_EVENT_TYPE = "now_available"

EMAIL_CHANNEL = "email"
PUSH_CHANNEL = "push"

Decision = tuple[UUID, str, str]
"""One row this pass may write: `(event_id, kind, channel)` — the unique key minus the user,
which every decision in a batch shares."""

PUSH_WHITELIST = tuple(sorted(ALWAYS_ON_ALERT_TYPES | {NOW_AVAILABLE_EVENT_TYPE}))
"""D-32 in full. Everything outside it is digest material; nothing `unconfirmed` is in it at
all, because the window admits only `confidence = 'confirmed'` before the type is even read."""

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


def deliverable_events(since: datetime) -> Select[tuple[UUID]]:
    """`SELECT event.id` for every event this pass may tell *anyone* about.

    Written to be used as `Event.id.in_(deliverable_events(since))`, so both branches and the
    counter share one definition of "in scope" rather than three.

    **The window.** `created_at` rather than `occurred_at`, because publication is the axis the
    product already groups and paginates on (ADR-0016) — an event carded today about a change
    TMDB recorded last week is news to the reader today, and dating the window by `occurred_at`
    would mail nobody about it. `confidence = 'confirmed'` is D-32's floor and sits here rather
    than in the alert branch: a `rumored` event is not digest material either, so nothing
    `unconfirmed` is ever queued in any kind. `status = 'published'` leaves out a superseded
    card in favour of the correction that replaced it.

    **The visibility terms are the three the feed applies**, and they are not decoration. A
    notification is a claim, made in the user's inbox, about a card they will then click
    through to — so queueing one the product will not show them is worse than queueing
    nothing. Each cuts a real case:

    - the `EventSummary` join, because an event with no summary has no copy for a sender to put
      in a mail. Hidden types are never summarized, so this and `visible_events()` overlap —
      but only the join states the sender's actual precondition.
    - `visible_events()`, so the `other` catch-all bucket (`news.visibility`) stays out.
    - `region_visible()`, so an Indian release-date change does not mail every watchlist holder
      about a date no surface will show them — D-32 is "US theatrical or home-release". This is
      the term that needs `Film` in the query.
    - `Film.slug.is_not(None)`, because a film with no slug has no page to link to.

    `correlate(None)` for the reason `app.follow_queries` gives: the queries this is dropped
    into select from `news.event` themselves, and the builder's meaning must not depend on the
    one it lands in.
    """
    return (
        select(Event.id)
        .join(EventSummary, EventSummary.event_id == Event.id)
        .join(Film, Film.id == Event.film_id)
        .where(
            Event.created_at > since,
            Event.status == "published",
            Event.confidence == "confirmed",
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


async def alert_event_ids(
    session: AsyncSession,
    *,
    recipient: Recipient,
    since: datetime,
    today: date,
    max_age_days: int,
) -> list[UUID]:
    """The window's events this user's **watchlist** earns an alert for (D-32, D-42).

    The watchlist is `follow_queries.watchlist_film_ids` and nothing else: the films this
    user's follows cover, inside the alert window, minus their mutes. Taking
    the builder rather than restating the rule is what keeps this pass, `/me/watchlist`, the
    calendar and the slate agreeing about one set.

    The whitelist is applied in SQL; the `now_available` store check is not, because it
    compares two arrays through a vocabulary mapping and reads far better as one named
    predicate than as a `CASE` over `unnest`. The SQL half has already cut the rows to this
    user's watchlist and this window, so what Python filters is a handful of events, not the
    ledger."""
    rows = await session.execute(
        select(Event.id, Event.event_type, Event.subject_key)
        .where(
            Event.film_id.in_(
                watchlist_film_ids(
                    user_id=recipient.user_id,
                    today=today,
                    max_age_days=max_age_days,
                )
            ),
            Event.event_type.in_(PUSH_WHITELIST),
            Event.id.in_(deliverable_events(since)),
        )
        .order_by(Event.created_at, Event.id)
    )
    return [
        row.id
        for row in rows
        if row.event_type in ALWAYS_ON_ALERT_TYPES
        or now_available_matches_stores(row.subject_key, recipient.alert_stores)
    ]


async def digest_event_ids(
    session: AsyncSession,
    *,
    user_id: UUID,
    since: datetime,
    today: date,
    excluded_statuses: frozenset[str],
) -> list[UUID]:
    """The window's events this user's follows reach — their timeline, restricted to what is
    new (D-11, D-33).

    Both halves of D-11, OR-ed exactly as `public.service.get_timeline` OR-s them: the films
    the follow graph reaches, and the events that *name* a followed person on a film they hold
    no credit on. Reusing the builders rather than restating the rule is the point — a digest
    that quietly covered less than the timeline it summarises would be the drift
    `app.follow_queries` exists to prevent.

    No event type is excluded here. Everything the timeline shows is digest material, including
    the whitelist beats: a user who follows the director *and* watchlists the film is owed the
    alert now and the line in their weekly slate, which is what the unique key's `kind` column
    is for."""
    rows = await session.execute(
        select(Event.id)
        .where(
            Event.id.in_(deliverable_events(since)),
            or_(
                Event.film_id.in_(
                    followed_film_ids(
                        user_id=user_id, today=today, excluded_statuses=excluded_statuses
                    )
                ),
                Event.id.in_(events_naming_followed_people(user_id)),
            ),
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
    today: date,
    excluded_statuses: frozenset[str],
    max_age_days: int,
) -> list[Decision]:
    """Both branches for one user, written in one statement. Returns the decisions written.

    The alert branch produces two rows per event for a user with a registered browser (D-36):
    the same event, the same `alert` kind, once per channel. Deliberately derived from the one
    list rather than queried twice — "the push whitelist" is D-32's list, and a push branch
    that selected its own events would be free to drift from the mail that accompanies it."""
    alerts = await alert_event_ids(
        session,
        recipient=recipient,
        since=since,
        today=today,
        max_age_days=max_age_days,
    )
    digests = await digest_event_ids(
        session,
        user_id=recipient.user_id,
        since=since,
        today=today,
        excluded_statuses=excluded_statuses,
    )
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
    today: date,
    excluded_statuses: frozenset[str],
    max_age_days: int,
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
                written = await decide_for_user(
                    s,
                    recipient=recipient,
                    since=since,
                    today=today,
                    excluded_statuses=excluded_statuses,
                    max_age_days=max_age_days,
                )
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
