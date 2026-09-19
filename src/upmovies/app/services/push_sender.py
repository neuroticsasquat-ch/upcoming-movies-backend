"""The push send pass: turning `queued` push rows into notifications, and recording each one.

Runs at the end of the `notify` slot beside the alert sender (`app.services.alert_sender`),
over the rows the decision pass wrote with `channel = 'push'` (D-36). It is that module's
sibling and deliberately shares its rules — the gate is re-read at send time, a row nothing can
ever send is `failed` with a reason rather than left `queued`, `failed` is terminal, and a
provider having a bad run aborts the pass rather than converting the whole backlog.

**One notification per event, fanned out over the user's browsers** — where the mail is one
message per user carrying every event. The difference is the medium, not an inconsistency: a
mail is a page that can hold three films, a push is a line on a lock screen, and three beats
collapsed into "3 updates" would cost the reader the one thing the notification is for. Fanning
out over subscriptions is not a choice at all: each body is encrypted to its own browser's key
(RFC 8291), so two devices are two sends by construction.

**A row's outcome is the best outcome any of its browsers had.** A user with a phone and a
laptop where only the laptop accepted the push has been notified, so the row is `sent`; it is
`failed` only when every subscription refused it, or when there is nothing left to send to.
The alternative — a row per subscription — would need a column the queue does not have, and
would make "did this user hear about it?" a question no single row answers.

**404 and 410 delete the subscription, and that is the only thing that does** apart from the
user unsubscribing (D-40). Those two statuses are the push service saying the endpoint is
permanently gone — a cleared browser profile, an expired registration — so keeping the row
would mean retrying a dead endpoint nightly forever. Every other refusal (429, 500, a timeout)
leaves the subscription alone, because it is a statement about this minute rather than about
the registration.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.models import Notification, PushSubscription, User
from upmovies.app.repos import push_subscription_repo
from upmovies.app.services.alert_sender import beat_label, film_url, mark, poster_url
from upmovies.app.verification import verified_user_clause
from upmovies.catalog.models import Film
from upmovies.config import Settings
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.news.models import Event, EventSummary
from upmovies.push import Pusher, PushError, PushSubscriptionGone, PushSubscriptionInfo

log = logging.getLogger(__name__)

ALERT_KIND = "alert"
PUSH_CHANNEL = "push"

NO_SUBSCRIPTIONS = "the user has no push subscription"
"""Why a row fails when every browser it could have gone to is gone.

It reads as an operational fact rather than an error because that is what it is: the user
unsubscribed, or every endpoint they had has been pruned, between the decision and the send."""


@dataclass(frozen=True)
class PushItem:
    """One event as one notification, already rendered.

    The payload is built when the backlog is read rather than at send time, so that the whole
    of what a notification says comes out of the one query that joined the film and the
    summary — the same argument the alert sender makes for its template context."""

    notification_id: UUID
    payload: str

    @staticmethod
    def build(*, title: str, beat: str, summary: str, url: str, icon: str | None) -> str:
        """The JSON body the service worker reads (NEU-1388).

        Flat and small on purpose: a push service caps the encrypted payload (4 KB is the
        conservative figure), and everything a notification needs is a title, a line of copy,
        somewhere to go and an image. The beat prefixes the title rather than occupying a field
        of its own, because a notification has two lines of chrome to work with and one of them
        is the app name."""
        return json.dumps(
            {"title": f"{beat}: {title}", "body": summary, "url": url, "icon": icon},
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class PushBatch:
    """Everything one user is owed by push in one run, and where it can be sent."""

    user_id: UUID
    deliverable: bool
    """Verified *and* entitled, re-read at send time — the queue outlives the run that wrote
    it, so a grant that lapsed overnight is exactly the case this catches (D-39)."""
    subscriptions: tuple[PushSubscriptionInfo, ...]
    items: tuple[PushItem, ...]
    unsendable: tuple[tuple[UUID, str], ...]
    """`(notification id, why)` for rows this pass can never send: the event lost its summary,
    or it is no longer the published card."""


@dataclass
class PushSendResult:
    """What one push send pass carried."""

    users_considered: int = 0
    notifications_sent: int = 0
    """Rows that reached at least one browser. Named for the row rather than for the send,
    because `pushes` below counts the sends."""
    pushes_delivered: int = 0
    """Individual encrypted messages a push service accepted — one per (row, browser). Higher
    than `notifications_sent` for a user with more than one device, which is the number that
    explains the slot's outbound traffic."""
    failed: int = 0
    suppressed: int = 0
    subscriptions_pruned: int = 0
    """Rows deleted because a push service answered 404/410 (D-36). Reported because a number
    that climbs here every night is a client that keeps re-subscribing browsers it then
    discards, which nothing else would show."""
    failures: int = 0
    """Batches lost to a crash *around* the send, whose rows stay `queued`."""
    aborted: bool = False
    abort_error: str | None = None


async def load_push_backlog(session: AsyncSession, *, settings: Settings) -> list[PushBatch]:
    """Every `queued` push alert, grouped into one batch per user, with that user's browsers.

    Two queries rather than one join: a user's subscriptions multiply their notifications, and
    fetching five events against three devices as fifteen rows to de-duplicate in Python buys
    nothing over asking for each list once.

    The notification read mirrors the alert sender's exactly — same joins, same two re-read
    columns (`Event.status`, the summary) — because the two passes are sending the same claim
    about the same event and must not disagree about whether it is still one worth making."""
    rows = await session.execute(
        select(
            Notification.id,
            Notification.user_id,
            and_(entitled_user_clause(), verified_user_clause()).label("deliverable"),
            Event.event_type,
            Event.status.label("event_status"),
            EventSummary.summary,
            Film.tmdb_id,
            Film.title,
            Film.poster_path,
        )
        .join(User, User.id == Notification.user_id)
        .join(Event, Event.id == Notification.event_id)
        .join(Film, Film.id == Event.film_id)
        .outerjoin(EventSummary, EventSummary.event_id == Event.id)
        .where(
            Notification.status == "queued",
            Notification.kind == ALERT_KIND,
            Notification.channel == PUSH_CHANNEL,
        )
        .order_by(Notification.user_id, Notification.created_at, Notification.id)
    )
    items: dict[UUID, list[PushItem]] = {}
    unsendable: dict[UUID, list[tuple[UUID, str]]] = {}
    deliverable: dict[UUID, bool] = {}
    for row in rows:
        deliverable.setdefault(row.user_id, row.deliverable)
        items.setdefault(row.user_id, [])
        unsendable.setdefault(row.user_id, [])
        if row.event_status != "published":
            unsendable[row.user_id].append((row.id, "the event is no longer published"))
            continue
        if row.summary is None:
            unsendable[row.user_id].append((row.id, "the event has no summary"))
            continue
        items[row.user_id].append(
            PushItem(
                notification_id=row.id,
                payload=PushItem.build(
                    title=row.title,
                    beat=beat_label(row.event_type),
                    summary=row.summary,
                    url=film_url(row.tmdb_id, row.title, settings.public_base_url),
                    icon=poster_url(row.poster_path, settings.tmdb_image_base),
                ),
            )
        )
    if not deliverable:
        return []
    subscriptions = await load_subscriptions(session, user_ids=list(deliverable))
    return [
        PushBatch(
            user_id=user_id,
            deliverable=is_deliverable,
            subscriptions=tuple(subscriptions.get(user_id, ())),
            items=tuple(items[user_id]),
            unsendable=tuple(unsendable[user_id]),
        )
        for user_id, is_deliverable in deliverable.items()
    ]


async def load_subscriptions(
    session: AsyncSession, *, user_ids: Sequence[UUID]
) -> dict[UUID, list[PushSubscriptionInfo]]:
    """Every browser registered by the users in the backlog, grouped by user."""
    rows = await session.execute(
        select(
            PushSubscription.user_id,
            PushSubscription.endpoint,
            PushSubscription.p256dh,
            PushSubscription.auth,
        )
        .where(PushSubscription.user_id.in_(user_ids))
        .order_by(PushSubscription.user_id, PushSubscription.created_at, PushSubscription.id)
    )
    grouped: dict[UUID, list[PushSubscriptionInfo]] = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(
            PushSubscriptionInfo(endpoint=row.endpoint, p256dh=row.p256dh, auth=row.auth)
        )
    return grouped


@dataclass
class BatchOutcome:
    """What one batch did to its rows, and whether the push service is the reason."""

    sent: int = 0
    delivered: int = 0
    failed: int = 0
    suppressed: int = 0
    pruned: int = 0
    provider_failed: bool = False
    """A push service refused a send for a reason other than the endpoint being gone. The
    only refusal that says anything about the *next* batch, so it is the only one the abort
    guard counts — an endpoint that is gone is evidence about one browser."""


async def send_batch(session: AsyncSession, *, batch: PushBatch, pusher: Pusher) -> BatchOutcome:
    """Push one user's queued alerts to every browser they have registered.

    **The gate is answered before anything else**, exactly as in the alert sender: a user who
    may not be notified gets `suppressed` on every row they hold, including the ones with no
    copy to send. Their subscriptions are left alone — a lapsed grant is not a dead endpoint
    (D-40).

    A subscription the push service has forgotten is deleted *inside this batch's session*, so
    a later row in the same batch does not try it again and the deletion commits with the
    outcomes it explains.

    The caller commits."""
    unsendable_ids = [row_id for row_id, _ in batch.unsendable]
    item_ids = [item.notification_id for item in batch.items]
    if not batch.deliverable:
        await mark(session, unsendable_ids + item_ids, status="suppressed")
        return BatchOutcome(suppressed=len(unsendable_ids) + len(item_ids))
    by_reason: dict[str, list[UUID]] = {}
    for row_id, why in batch.unsendable:
        by_reason.setdefault(why, []).append(row_id)
    for why, row_ids in by_reason.items():
        await mark(session, row_ids, status="failed", error=why)
    outcome = BatchOutcome(failed=len(unsendable_ids))
    if not item_ids:
        return outcome
    live = list(batch.subscriptions)
    for item in batch.items:
        if not live:
            # Either the user never had a browser registered by the time the send ran, or
            # every one of them has just been pruned. Both are "there is nowhere to send
            # this", and a row nothing can ever deliver must not stay `queued`.
            await mark(session, [item.notification_id], status="failed", error=NO_SUBSCRIPTIONS)
            outcome.failed += 1
            continue
        delivered = 0
        errors: list[str] = []
        for subscription in list(live):
            try:
                await pusher.send(subscription=subscription, payload=item.payload)
            except PushSubscriptionGone as exc:
                log.info("pruning gone push subscription for user_id=%s", batch.user_id)
                live.remove(subscription)
                outcome.pruned += await push_subscription_repo.delete_by_endpoint(
                    session, endpoint=subscription.endpoint
                )
                errors.append(str(exc))
            except PushError as exc:
                log.warning("push to user_id=%s failed: %s", batch.user_id, exc)
                errors.append(str(exc))
                outcome.provider_failed = True
            else:
                delivered += 1
        if delivered:
            await mark(session, [item.notification_id], status="sent", sent_at=datetime.now(UTC))
            outcome.sent += 1
            outcome.delivered += delivered
        else:
            await mark(
                session,
                [item.notification_id],
                status="failed",
                error="; ".join(errors) or NO_SUBSCRIPTIONS,
            )
            outcome.failed += 1
    return outcome


async def send_queued_pushes(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    pusher: Pusher,
    settings: Settings,
    failure_threshold: int = 10,
) -> PushSendResult:
    """Push every `queued` push alert, one batch per user, and record each row's outcome.

    The same pipeline conventions as every other pass here: one session per user so a failure
    never rolls back the others, `record_progress` against the run id, abort after N
    consecutive failures, and **no** `finalize_run` — the run belongs to
    `pipeline_run.run_notify_stage`, which runs all three passes under one row.

    The abort guard counts push-service refusals for the reason the alert sender counts
    provider refusals: a broken VAPID key or a push service having an outage is one shared
    fault, and grinding the whole backlog into `failed` rows against it under a green check is
    the failure worth preventing. An endpoint that is *gone* is explicitly not counted — that
    is a normal, expected event that prunes one row and proves nothing about the next user."""
    result = PushSendResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        batches = await load_push_backlog(s, settings=settings)
    result.users_considered = len(batches)
    log.info("notify: %d users with queued push alerts", result.users_considered)

    for batch in batches:
        await heartbeat.tick()
        try:
            async with owned_session(session_factory) as s:
                outcome = await send_batch(s, batch=batch, pusher=pusher)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except Exception:
            # A crash *around* the send. The rows stay `queued` and the next run carries
            # them, with the same honest caveat the alert sender states: a crash after a push
            # service accepted the message means the reader sees it twice. A duplicate
            # notification is the better of the two failures.
            log.exception("pushing alerts to user %s failed", batch.user_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"push send aborted after {guard.consecutive} failures"
                log.error("notify: %s", result.abort_error)
                return result
            continue
        result.notifications_sent += outcome.sent
        result.pushes_delivered += outcome.delivered
        result.failed += outcome.failed
        result.suppressed += outcome.suppressed
        result.subscriptions_pruned += outcome.pruned
        if outcome.provider_failed:
            if await guard.failed():
                result.aborted = True
                result.abort_error = (
                    f"push send aborted after {guard.consecutive} consecutive provider failures"
                )
                log.error("notify: %s", result.abort_error)
                return result
            continue
        guard.succeeded()

    log.info(
        "notify: %d push notifications (%d deliveries), %d failed, %d suppressed, "
        "%d subscriptions pruned, %d lost",
        result.notifications_sent,
        result.pushes_delivered,
        result.failed,
        result.suppressed,
        result.subscriptions_pruned,
        result.failures,
    )
    return result


def push_send_detail(result: PushSendResult) -> str:
    """The push half of the notify run's `ingest_run.detail` line, appended after the alert
    sender's."""
    line = (
        f"push: {result.notifications_sent} notifications to {result.users_considered} users, "
        f"{result.pushes_delivered} delivered, {result.failed} failed, "
        f"{result.suppressed} suppressed, {result.subscriptions_pruned} pruned, "
        f"{result.failures} lost"
    )
    if result.aborted:
        line += f"; {result.abort_error}"
    return line
