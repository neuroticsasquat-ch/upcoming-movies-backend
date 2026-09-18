"""The decision pass: who is owed what about the events published since last time (D-31).

`python -m upmovies.pipeline_run notify` runs this on its own Coolify slot, after the daily
chain. It is the only writer of `app.notification` — ingest never calls it, and no route does
either — because the decision is a fan-out over *every* user, and a route only ever knows about
the one who made the request.

**Two branches, from INV-7's two halves.** A watchlist item is the only row that produces a
push, so the alert branch reads `app.watchlist_item` and admits only D-32's whitelist. A follow
produces timeline rows, so the digest branch reads the follow graph through
`app.follow_queries` — the same builders `/me/timeline` hands to the feed, which is what keeps
"in my digest" and "on my timeline" from drifting into two answers. A user who both follows
and watchlists a film gets both rows: an alert and a digest line are different deliveries of
the same news, not duplicates of one (`app.models.Notification`).

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

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.follow_queries import events_naming_followed_people, followed_film_ids
from upmovies.app.models import Follow, Notification, User, WatchlistItem
from upmovies.app.verification import verified_user_clause
from upmovies.ingest.runs import last_successful_run_started_at, record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.news.models import Event

log = logging.getLogger(__name__)

NOTIFY_RUN_KIND = "notify"
"""This pass's own `ingest_run.kind` — the watermark is read from the last run that carried it,
so the name is part of the contract rather than a label."""

ALWAYS_ON_ALERT_TYPES = frozenset({"release_date", "trailer"})
"""The whitelist beats a watchlist item cannot switch off (D-32, `WatchlistItem.alert_prefs`).
A date assigned or moved and a new trailer are *why* the film is on the list."""

NOW_AVAILABLE_EVENT_TYPE = "now_available"

PUSH_WHITELIST = tuple(sorted(ALWAYS_ON_ALERT_TYPES | {NOW_AVAILABLE_EVENT_TYPE}))
"""D-32 in full. Everything outside it is digest material; nothing `unconfirmed` is in it at
all, because the window admits only `confidence = 'confirmed'` before the type is even read."""

ALERT_PREF_BY_MONETIZATION = {"flatrate": "stream", "rent": "rent", "buy": "buy"}
"""The one place `catalog.MONETIZATION_TYPES` and `app.models.ALERT_PREFS` meet.

They disagree on one word: TMDB calls a subscription offer `flatrate` (and so does
`availability_first_seen`, and so do the `US:flatrate` tokens `now_available` writes into
`subject_key`), while D-14 spells the user-facing preference `stream`. Mapping them here, once,
is what stops a `{stream}` watchlist item — the server default, and so the setting most users
will have — from silently matching nothing at all. `tests/unit/app/test_notify_decisions.py`
pins the mapping to `MONETIZATION_TYPES`, so a fourth offer kind fails a test rather than
quietly alerting nobody about itself."""


@dataclass
class NotifyResult:
    """What one decision pass considered and wrote."""

    users_considered: int = 0
    """Users holding a follow or a watchlist item — the pass's working set, not the user table.
    Reported because `0 queued` is a perfectly healthy quiet day and says nothing on its own
    about whether the pass had anybody to decide for."""
    events_considered: int = 0
    alerts_queued: int = 0
    digests_queued: int = 0
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

    @property
    def status(self) -> str:
        return "queued" if self.deliverable else "suppressed"


def now_available_matches_prefs(
    subject_key: Sequence[str] | None, alert_prefs: Sequence[str]
) -> bool:
    """Whether a `now_available` card names an availability type this item wants (D-14, D-28).

    The card's `subject_key` carries one `region:monetization` token per type the film was
    newly seen under — one card can announce rent *and* buy in a single observation — so this
    asks whether *any* of them is wanted, not whether all are.

    A token whose monetization half is unrecognised matches nothing rather than raising: this
    runs over the whole ledger on a schedule, and one malformed key must not cost every user
    their notifications for the day. An empty or absent `subject_key` is such a card with no
    types at all, and matches nothing on the same terms."""
    wanted = set(alert_prefs)
    return any(
        ALERT_PREF_BY_MONETIZATION.get(token.partition(":")[2]) in wanted
        for token in subject_key or ()
    )


def _published_window(since: datetime) -> ColumnElement[bool]:
    """The events this pass decides about: published, confirmed, and new since the watermark.

    `created_at` rather than `occurred_at` because publication is the axis the product already
    groups and paginates on (ADR-0016) — an event carded today about a change TMDB recorded
    last week is news to the reader today, and dating the window by `occurred_at` would mail
    nobody about it.

    `confidence = 'confirmed'` is D-32's floor and sits here, in the window, rather than in the
    alert branch: a `rumored` event is not digest material either, and nothing `unconfirmed` is
    ever queued in any kind."""
    return and_(
        Event.created_at > since,
        Event.status == "published",
        Event.confidence == "confirmed",
    )


async def load_recipients(session: AsyncSession) -> list[Recipient]:
    """Every user with a follow or a watchlist item, oldest account first.

    Both the entitlement and the verification rule are read here, as columns rather than as
    filters, because this pass owes a `suppressed` row to the users they exclude (D-39) —
    filtering them out in SQL is exactly the silent skip the decision is meant to replace.

    The `EXISTS` pair is what keeps the pass proportional to the graph rather than to signups:
    an account that follows nothing and watchlists nothing is owed no decision by definition,
    and selecting it would buy three statements per signup on every run."""
    rows = await session.execute(
        select(
            User.id,
            and_(entitled_user_clause(), verified_user_clause()).label("deliverable"),
        )
        .where(
            or_(
                exists().where(Follow.user_id == User.id),
                exists().where(WatchlistItem.user_id == User.id),
            )
        )
        .order_by(User.created_at, User.id)
    )
    return [Recipient(user_id=row.id, deliverable=row.deliverable) for row in rows]


async def alert_event_ids(session: AsyncSession, *, user_id: UUID, since: datetime) -> list[UUID]:
    """The window's events this user's watchlist earns an alert for (D-32).

    The whitelist is applied in SQL; the `now_available` preference check is not, because it
    compares two arrays through a vocabulary mapping and reads far better as one named
    predicate than as a `CASE` over `unnest`. The SQL half has already cut the rows to this
    user's watchlist and this window, so what Python filters is a handful of events, not the
    ledger."""
    rows = await session.execute(
        select(Event.id, Event.event_type, Event.subject_key, WatchlistItem.alert_prefs)
        .join(WatchlistItem, WatchlistItem.film_id == Event.film_id)
        .where(
            WatchlistItem.user_id == user_id,
            Event.event_type.in_(PUSH_WHITELIST),
            _published_window(since),
        )
        .order_by(Event.created_at, Event.id)
    )
    return [
        row.id
        for row in rows
        if row.event_type in ALWAYS_ON_ALERT_TYPES
        or now_available_matches_prefs(row.subject_key, row.alert_prefs)
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
            _published_window(since),
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
    session: AsyncSession, *, recipient: Recipient, decisions: Sequence[tuple[UUID, str]]
) -> list[str]:
    """Write one decision per `(event_id, kind)` and return the kinds actually written.

    `ON CONFLICT DO NOTHING` against `uq_notification_user_event_kind_channel` is what makes a
    re-run free, and `RETURNING` is what makes the counts honest: a second pass over the same
    window reports nothing queued because nothing was, rather than because the pass declined to
    look.

    `channel = 'email'` throughout. Push is the same queue with a second channel, and it ships
    last (D-36) — a `push` row written before a `push_subscription` table exists would be a
    delivery nobody can make."""
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
                    "channel": "email",
                    "status": recipient.status,
                }
                for event_id, kind in decisions
            ]
        )
        .on_conflict_do_nothing(index_elements=["user_id", "event_id", "kind", "channel"])
        .returning(Notification.kind)
    )
    return list(rows.scalars().all())


async def decide_for_user(
    session: AsyncSession,
    *,
    recipient: Recipient,
    since: datetime,
    today: date,
    excluded_statuses: frozenset[str],
) -> list[str]:
    """Both branches for one user, written in one statement. Returns the kinds written."""
    alerts = await alert_event_ids(session, user_id=recipient.user_id, since=since)
    digests = await digest_event_ids(
        session,
        user_id=recipient.user_id,
        since=since,
        today=today,
        excluded_statuses=excluded_statuses,
    )
    decisions = [(event_id, "alert") for event_id in alerts]
    decisions += [(event_id, "digest") for event_id in digests]
    return await queue_decisions(session, recipient=recipient, decisions=decisions)


async def run_notify_pass(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    today: date,
    excluded_statuses: frozenset[str],
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
            await s.execute(select(func.count()).select_from(Event).where(_published_window(since)))
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
            result.alerts_queued += written.count("alert")
            result.digests_queued += written.count("digest")
        else:
            result.suppressed += len(written)

    log.info(
        "notify: %d alerts, %d digests, %d suppressed, %d failed",
        result.alerts_queued,
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
        f"{result.alerts_queued} alerts, {result.digests_queued} digests, "
        f"{result.suppressed} suppressed, {result.failures} failed"
    )
    if result.aborted:
        line += f"; notify aborted: {result.abort_error}"
    return line
