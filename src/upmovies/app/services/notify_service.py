"""The decision pass: who is owed what about the events published since last time (D-31).

`python -m upmovies.pipeline_run notify` runs this on its own Coolify slot, after the daily
chain. It is the only writer of `app.notification` — ingest never calls it, and no route does
either — because the decision is a fan-out over *every* user, and a route only ever knows about
the one who made the request.

**One branch, one clause (EF-3, ADR-0021).** The digest is the only delivery, and it reads the
same two builders `/me/timeline` hands to the feed — `title_follow_film_ids` and
`entity_attachment_event_ids` — OR-ed exactly as the timeline OR-s them (`follow_scope`), which
is what keeps "in my digest" and "on my timeline" from drifting into two answers. An
**unfollowed** film earns nothing, and there is nothing else that subtracts: the mute went with
the watchlist it corrected (EF-14), so this pass reads exactly what the follow builders select
and needs no rule of its own. Nor is there a whitelist, a confirmation gate or a per-beat
preference: a `rumored` card is a digest line marked Unconfirmed (DC-5), and a reader hears
about any beat at the cadence they chose and no sooner.

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

from sqlalchemy import Select, and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.follow_queries import follow_scope
from upmovies.app.models import Follow, Notification, User
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

EMAIL_CHANNEL = "email"

Decision = tuple[UUID, str, str]
"""One row this pass may write: `(event_id, kind, channel)` — the unique key minus the user,
which every decision in a batch shares. Both kind and channel are fixed now (`digest`,
`email`; ADR-0021), but they are the key's columns and the row keeps its shape."""


@dataclass
class NotifyResult:
    """What one decision pass considered and wrote."""

    users_considered: int = 0
    """Users holding a follow — the pass's working set, not the user table.
    Reported because `0 queued` is a perfectly healthy quiet day and says nothing on its own
    about whether the pass had anybody to decide for."""
    events_considered: int = 0
    digests_queued: int = 0
    suppressed: int = 0
    """Rows written for a user who may not be mailed — unverified (D-31) or unentitled (D-39).
    Counted apart from `digests_queued` because a number that climbs here while that one stays
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


def deliverable_events(since: datetime) -> Select[tuple[UUID]]:
    """`SELECT event.id` for every event this pass may tell *anyone* about.

    Written to be used as `Event.id.in_(deliverable_events(since))`, so the digest branch and
    the counter share one definition of "in scope" rather than two.

    **The window.** `created_at` rather than `occurred_at`, because publication is the axis the
    product already groups and paginates on (ADR-0016) — an event carded today about a change
    TMDB recorded last week is news to the reader today, and dating the window by `occurred_at`
    would mail nobody about it. `status = 'published'` leaves out a superseded card in favour of
    the correction that replaced it.

    Re-considering an event is cheap and idempotent — `queue_decisions` writes through
    `uq_notification_user_event_kind_channel`, so a card that already earned this user a digest
    line earns nothing the second time.

    **No confidence floor** (D-1437.7). It used to sit here, on the grounds that a `rumored`
    event was not digest material either — but every catalog attach and detach card is
    `rumored` until its quarantine clears, so with the floor in the shared selector an entity
    follower's digest would carry nothing their follow delivers. EF-7 is that the digest
    carries everything the timeline carries, and since ADR-0021 retired the push nothing waits
    for confirmation at all: a `rumored` card is a digest line marked Unconfirmed (DC-5).

    **The visibility terms are the three the feed applies**, and they are not decoration. A
    notification is a claim, made in the user's inbox, about a card they will then click
    through to — so queueing one the product will not show them is worse than queueing
    nothing. Each cuts a real case:

    - the `EventSummary` join, because an event with no summary has no copy for a sender to put
      in a mail. Hidden types are never summarized, so this and `visible_events()` overlap —
      but only the join states the sender's actual precondition.
    - `visible_events()`, so the `other` catch-all bucket (`news.visibility`) stays out.
    - `region_visible()`, so an Indian release-date change does not mail every follower
      about a date no surface will show them. This is the term that needs `Film` in the query.
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
    filtering them out in SQL is exactly the silent skip the decision is meant to replace."""
    rows = await session.execute(
        select(
            User.id,
            and_(entitled_user_clause(), verified_user_clause()).label("deliverable"),
        )
        .where(exists().where(Follow.user_id == User.id))
        .order_by(User.created_at, User.id)
    )
    return [Recipient(user_id=row.id, deliverable=row.deliverable) for row in rows]


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
    material, including a `rumored` attachment, which the digest marks Unconfirmed (DC-5)."""
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

    The kind and channel are carried on each decision rather than fixed here because they are
    the unique key's columns, even with one value each (ADR-0021). The `event_id` is returned
    alongside so a caller could group by event; the counters only count rows."""
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
    """The digest branch for one user, written in one statement. Returns the decisions
    written."""
    digests = await digest_event_ids(session, user_id=recipient.user_id, since=since)
    decisions: list[Decision] = [(event_id, "digest", EMAIL_CHANNEL) for event_id in digests]
    return await queue_decisions(session, recipient=recipient, decisions=decisions)


async def run_notify_pass(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    failure_threshold: int = 10,
) -> NotifyResult:
    """Decide what every user is owed about the events published since the last successful run.

    **The first run ever queues nothing.** With no successful predecessor there is no watermark,
    and "everything since the beginning of time" is the whole ledger — a first deploy that
    queued a digest line for every release date the catalogue has ever recorded. So a cold start
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
            result.digests_queued += len(written)
        else:
            result.suppressed += len(written)

    log.info(
        "notify: %d digests, %d suppressed, %d failed",
        result.digests_queued,
        result.suppressed,
        result.failures,
    )
    return result


def notify_detail(result: NotifyResult) -> str:
    """The run's `ingest_run.detail` line.

    `suppressed` sits beside the queued count rather than folded into a total, for the reason
    the field itself gives: it is the access gate's own counter, and an operator reading a run
    that queued nothing needs to know at a glance whether nobody was owed anything or nobody
    was allowed anything."""
    if result.cold_start:
        return "notify: cold start — watermark established, nothing queued"
    line = (
        f"notify: {result.events_considered} events, {result.users_considered} users, "
        f"{result.digests_queued} digests, "
        f"{result.suppressed} suppressed, {result.failures} failed"
    )
    if result.aborted:
        line += f"; notify aborted: {result.abort_error}"
    return line
