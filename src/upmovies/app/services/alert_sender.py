"""The send pass: turning `queued` alert rows into mail, and recording what became of each.

Runs at the end of the `notify` slot, immediately after the decision pass that wrote the rows
(`app.services.notify_service`). Two passes rather than one function for the reason D-31 puts
the queue in a table at all: deciding is a fan-out over every user and sending is a fan-out
over a provider, and a provider having a bad minute must not cost the decisions — the rows are
already committed, so the next run picks up exactly what did not go out.

**One mail per user per run, not one per event.** A user who follows three films that all
moved on the same day is owed one mail about three films, not three mails in ninety seconds.
So the backlog is read grouped by user, every row in a group rides on one send, and the
`Notification` rows in that group share its outcome. That is also what makes the failure
handling honest: a failed send marks *every* row it carried `failed`, because a row left
`queued` after its mail was attempted would be re-sent as part of somebody's next batch.

**The access gate is re-read here, not trusted from the queue** (D-31, D-39). A row is only
ever `queued` for a user who was verified and entitled when the decision pass looked, but the
queue survives a failed run, so "when the decision pass looked" can be yesterday — and an
`entitled_until` that lapsed overnight is exactly the case the gate exists for. The same two
named rules the decision pass uses answer it here, and a row that fails them becomes
`suppressed` rather than disappearing, for the reason suppression is a row at all: silence
and "we decided not to mail you" have to be distinguishable afterwards.

**Everything the mail says comes out of one query, and the parts that can change are re-read.**
The film's title and poster, the event's type, status and summary are what the `alert` template
renders, so the backlog read joins them rather than handing the template a row to go looking
for them from. Two of them are re-read for the same reason the access gate is: the queue
outlives the run that wrote it. An event superseded by a correction while its row sat `queued`
is no longer a claim to make in somebody's inbox, and an event whose summary has gone missing
has no copy to make it with. Neither is left `queued` — a permanently un-sendable row in a
backlog retried every night is the one shape of stall nothing else here would report.

**`failed` is terminal, and that is a real cost worth stating.** The backlog read admits only
`queued`, and the decision pass's re-insert is `ON CONFLICT DO NOTHING`, so a row this pass
marks `failed` is not reconsidered by either pass — recovering one means an operator re-queuing
it by hand. The bound on that loss is the abort guard in `send_queued_alerts`: a provider that
is refusing mail stops the pass within `failure_threshold` batches and fails the run, rather
than converting the whole backlog into failures under a green check. A single transient refusal
still costs that one user that one mail, which is the shape the ticket asked for ("mark `sent`
or `failed` with the error"); a bounded retry would need an attempt count on the row and is
deliberately not invented here.

**The copy is the summary's, including the parts the ticket enumerates.** D-32's whitelisted
beats are summarized deterministically (`synthesize.deterministic`), and those bodies already
name the date a release moved to and the services a film landed on — so the mail renders
`EventSummary.summary` rather than deriving either a second time, which would be a second copy
of phrasing rules that module exists to keep in one place. Note the edge this leaves: a
`release_date` event that a story later clusters onto has its deterministic body replaced by an
LLM summary, and nothing obliges that one to name the date. The guarantee is the summarizer's,
not this module's.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.models import Notification, User
from upmovies.app.verification import verified_user_clause
from upmovies.catalog.models import Film
from upmovies.catalog.ref import film_ref
from upmovies.config import Settings
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.mail import (
    MailConfigurationError,
    Mailer,
    MailError,
    MissingCredentialError,
)
from upmovies.news.models import Event, EventSummary

log = logging.getLogger(__name__)

ALERT_TEMPLATE = "alert"
"""The `mail/templates/` directory this pass renders. M7's contract names three templates —
`alert`, `digest`, `slate` — and this pass owns the first."""

ALERT_KIND = "alert"
EMAIL_CHANNEL = "email"

POSTER_SIZE = "w154"
"""TMDB's small poster width, and a deliberate floor. A mail is read on a phone over a mobile
connection and its images are fetched before the reader has decided they want them, so the
poster is a thumbnail beside the copy rather than the artwork it is on the film page."""

BEAT_LABELS = {
    "release_date": "Release date",
    "now_available": "Now available",
    "trailer": "New trailer",
}
"""What to call each whitelisted beat in the mail (D-32).

Only the three D-32 admits, because only those reach an `alert` row. `beat_label` falls back
rather than raising for the same reason `_render_status` does in `synthesize.deterministic`: a
fourth whitelisted type added to the decision pass and forgotten here should read plainly in
one mail, not fail every alert in the batch."""


def beat_label(event_type: str) -> str:
    """The mail's name for an event type. Never raises — see `BEAT_LABELS`."""
    return BEAT_LABELS.get(event_type, "Update")


@dataclass(frozen=True)
class AlertItem:
    """One event, as the `alert` template renders it."""

    notification_id: UUID
    title: str
    beat: str
    summary: str
    film_url: str
    poster_url: str | None

    def as_context(self) -> dict[str, object]:
        """The template's view of this item — deliberately without the notification id, which
        is bookkeeping and has no business being reachable from a template."""
        return {
            "title": self.title,
            "beat": self.beat,
            "summary": self.summary,
            "film_url": self.film_url,
            "poster_url": self.poster_url,
        }


@dataclass(frozen=True)
class AlertBatch:
    """Everything one user is owed in one run, and whether they may be mailed it."""

    user_id: UUID
    email: str
    display_name: str
    deliverable: bool
    """Verified *and* entitled, re-read at send time — see the module docstring."""
    items: tuple[AlertItem, ...]
    unsendable: tuple[tuple[UUID, str], ...]
    """`(notification id, why)` for every row this pass can never send: the event lost its
    summary, or it is no longer the published card. Marked `failed` with the reason rather
    than left `queued`, because a row nothing can ever send is a stall no counter would
    report — and the reason is the only thing that tells an operator which of the two it
    was."""


@dataclass
class AlertSendResult:
    """What one send pass carried."""

    users_considered: int = 0
    """Users holding at least one `queued` alert — the backlog's shape, not the user table."""
    mails_sent: int = 0
    """Mails actually handed to the provider. One per user, so this is the number of users
    who heard from us, and it is reported beside `sent` because the two answer different
    questions: `sent` is rows cleared, this is inboxes touched."""
    sent: int = 0
    failed: int = 0
    """Rows this pass marked `failed` — a provider that refused the mail, or an event with no
    sendable copy. Terminal: see `load_alert_backlog`."""
    suppressed: int = 0
    failures: int = 0
    """Batches lost to a crash *around* the send — the session, the bookkeeping write — as
    opposed to a provider refusing a mail, which is a `failed` row. Counted separately and
    reported on the detail line because their rows stay `queued` and say nothing themselves:
    without this number a pass that lost forty batches to a database having a bad minute
    reads as `0 failed` on `/admin/runs`."""
    aborted: bool = False
    abort_error: str | None = None


def poster_url(poster_path: str | None, image_base: str) -> str | None:
    """The absolute URL for a poster path, or None when the film has no poster.

    Absolute because a mail has no page to resolve a relative path against. None rather than a
    placeholder image: the template drops the poster cell entirely, which reads better than a
    grey box and costs the reader one fewer image fetch."""
    if not poster_path:
        return None
    return f"{image_base.rstrip('/')}/{POSTER_SIZE}{poster_path}"


def film_url(tmdb_id: int, title: str, base_url: str) -> str:
    """The film's public page — the same `/film/{ref}` the sitemap emits, built from the same
    `film_ref`, so a link in a mail cannot address a film differently from a link on the site
    (and cannot land on the 301 a bare id would)."""
    return f"{base_url.rstrip('/')}/film/{film_ref(tmdb_id, title)}"


def settings_url(base_url: str) -> str:
    """Where the mail's unsubscribe line points: the reader's own settings page, which is
    where `digest_cadence` and the alert preferences live (D-33, D-14).

    A settings link rather than a one-click unsubscribe token because an alert is not a
    broadcast — every one of them is something this reader's own follows reached, so the
    useful control is *which* beats and *which* films, not an all-or-nothing opt-out."""
    return f"{base_url.rstrip('/')}/settings"


async def load_alert_backlog(session: AsyncSession, *, settings: Settings) -> list[AlertBatch]:
    """Every `queued` alert awaiting email, grouped into one batch per user.

    The join is the mail: film title, poster and public ref, plus the event's type and
    summary. `EventSummary` is the one outer join — `film_id` and `user_id` are non-null FKs,
    so their rows are there by construction, but a summary can be absent (an event carded
    between the decision pass and a summarize run that has not happened, or a deleted row),
    and that has to come back as a *failed* notification rather than as a row this query
    quietly declines to return.

    Ordered by user and then by when the decision was made, so a batch's items read in the
    order the news arrived, and the batches themselves are stable between runs."""
    rows = await session.execute(
        select(
            Notification.id,
            Notification.user_id,
            User.email,
            User.display_name,
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
            Notification.channel == EMAIL_CHANNEL,
        )
        .order_by(Notification.user_id, Notification.created_at, Notification.id)
    )
    items: dict[UUID, list[AlertItem]] = {}
    unsendable: dict[UUID, list[tuple[UUID, str]]] = {}
    recipients: dict[UUID, tuple[str, str, bool]] = {}
    for row in rows:
        recipients.setdefault(row.user_id, (row.email, row.display_name, row.deliverable))
        items.setdefault(row.user_id, [])
        unsendable.setdefault(row.user_id, [])
        if row.event_status != "published":
            # The card was superseded by a correction while this row sat in the queue. The
            # decision pass admits only `published` events (`notify_service`), so this is the
            # queue having outlived that check — and a mail whose claim the product has
            # already replaced is worse than no mail at all.
            unsendable[row.user_id].append((row.id, "the event is no longer published"))
            continue
        if row.summary is None:
            unsendable[row.user_id].append((row.id, "the event has no summary"))
            continue
        items[row.user_id].append(
            AlertItem(
                notification_id=row.id,
                title=row.title,
                beat=beat_label(row.event_type),
                summary=row.summary,
                film_url=film_url(row.tmdb_id, row.title, settings.public_base_url),
                poster_url=poster_url(row.poster_path, settings.tmdb_image_base),
            )
        )
    return [
        AlertBatch(
            user_id=user_id,
            email=email,
            display_name=display_name,
            deliverable=deliverable,
            items=tuple(items[user_id]),
            unsendable=tuple(unsendable[user_id]),
        )
        for user_id, (email, display_name, deliverable) in recipients.items()
    ]


def alert_context(batch: AlertBatch, *, settings: Settings) -> dict[str, object]:
    """The `alert` template's context for one batch.

    The subject line is the template's own business (`subject.txt` is a template like the
    other two parts), which is why nothing here decides between "one film" and "three
    updates" — the copy for both lives in one file a person can read as a unit."""
    return {
        "product_name": settings.product_name,
        "display_name": batch.display_name,
        "settings_url": settings_url(settings.public_base_url),
        "items": [item.as_context() for item in batch.items],
    }


async def mark(
    session: AsyncSession,
    ids: Sequence[UUID],
    *,
    status: str,
    sent_at: datetime | None = None,
    error: str | None = None,
) -> None:
    """Record one outcome against a set of notification rows.

    `sent_at` and `error` are always written, including as NULL: a row an operator re-queued
    after a failure must not keep yesterday's error beside today's `sent_at`. (Re-queuing is
    the only route back — this pass never reconsiders a `failed` row itself; see the module
    docstring.)"""
    if not ids:
        return
    await session.execute(
        update(Notification)
        .where(Notification.id.in_(ids))
        .values(status=status, sent_at=sent_at, error=error)
    )


@dataclass(frozen=True)
class BatchOutcome:
    """What one batch did to its rows, and whether the provider is the reason."""

    sent: int = 0
    failed: int = 0
    suppressed: int = 0
    provider_failed: bool = False
    """The provider refused this mail. Distinguished from every other kind of `failed` row
    because it is the only one that says anything about the *next* batch: a provider is one
    shared dependency, so a refusal is evidence about the run, where an event with no copy is
    evidence about one event. `send_queued_alerts` counts these toward the abort guard and
    nothing else."""


async def send_batch(
    session: AsyncSession, *, batch: AlertBatch, mailer: Mailer, settings: Settings
) -> BatchOutcome:
    """Send one user's alerts and record every row's outcome.

    **The gate is answered before anything else.** A user who may not be mailed gets
    `suppressed` on every row they hold, including the ones with no copy to send: "we may not
    mail you" is the whole answer for that user, and marking their summaryless row `failed`
    would put a row in an operator's failure read that nothing ever intended to send.

    The provider's errors are caught here rather than allowed to propagate, and the exact set
    is the one `verification_service.send` catches, for the same reason: the two
    `RuntimeError`s are raised when the gateway *builds* its transport, which happens on the
    first send of the process. Boot validation (D-30) makes them unlikely, not impossible, and
    an unlikely configuration fault should mark a batch `failed` with a reason rather than
    crash the run that would have reported it. It is reported back rather than swallowed,
    because a provider that refuses one mail is about to refuse the next.

    The caller commits — `owned_session` gives each batch its own, so one user's outcome is
    written whatever happens to the next."""
    unsendable_ids = [row_id for row_id, _ in batch.unsendable]
    item_ids = [item.notification_id for item in batch.items]
    if not batch.deliverable:
        # No mail, and no error either: nothing went wrong, the gate answered no (D-39).
        await mark(session, unsendable_ids + item_ids, status="suppressed")
        return BatchOutcome(suppressed=len(unsendable_ids) + len(item_ids))
    # Grouped by reason so this is one statement per distinct reason (there are two) rather
    # than one per row.
    by_reason: dict[str, list[UUID]] = {}
    for row_id, why in batch.unsendable:
        by_reason.setdefault(why, []).append(row_id)
    for why, row_ids in by_reason.items():
        await mark(session, row_ids, status="failed", error=why)
    failed = len(unsendable_ids)
    if not item_ids:
        return BatchOutcome(failed=failed)
    try:
        await mailer.send(
            to=batch.email, template=ALERT_TEMPLATE, context=alert_context(batch, settings=settings)
        )
    except (MailError, MailConfigurationError, MissingCredentialError, httpx.HTTPError) as exc:
        log.exception("alert mail to user_id=%s failed", batch.user_id)
        await mark(session, item_ids, status="failed", error=f"{type(exc).__name__}: {exc}")
        return BatchOutcome(failed=failed + len(item_ids), provider_failed=True)
    await mark(session, item_ids, status="sent", sent_at=datetime.now(UTC))
    return BatchOutcome(sent=len(item_ids), failed=failed)


async def send_queued_alerts(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    mailer: Mailer,
    settings: Settings,
    failure_threshold: int = 10,
) -> AlertSendResult:
    """Mail every `queued` alert, one batch per user, and record each row's outcome.

    Shares the pipeline conventions the decision pass states: one session per user so a
    failure never rolls back the others, `record_progress` against the run id, abort after N
    consecutive failures, and **no** `finalize_run` — the notify run's status and detail line
    belong to `pipeline_run.run_notify_stage`, which runs both passes under one row.

    **The abort guard counts provider refusals, and that is the whole point of it here.** A
    mail provider is one shared dependency: ten consecutive users whose send was refused is
    one outage — a rotated key, a suspended account, Resend having a bad ten minutes — not ten
    unrelated faults. Left to run, the pass would spend the night turning the entire backlog
    into `failed` rows against a provider that was never going to accept any of them, and
    then report `succeeded` because nothing raised. So a refusal both fails that batch's rows
    *and* counts toward the threshold; crossing it stops the pass and fails the run, which is
    what puts the healthchecks deadman red on a night nobody got mail. Every batch the pass
    did not reach is untouched and still `queued`, so the loss is bounded by the threshold
    rather than by the size of the backlog.

    A crash *around* the send — the session, the bookkeeping write — counts toward the same
    threshold but leaves its rows `queued`; those batches are `failures` rather than `failed`
    rows, and the two are reported separately because only one of them is terminal.
    """
    result = AlertSendResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        batches = await load_alert_backlog(s, settings=settings)
    result.users_considered = len(batches)
    log.info("notify: %d users with queued alerts to mail", result.users_considered)

    for batch in batches:
        await heartbeat.tick()
        try:
            async with owned_session(session_factory) as s:
                outcome = await send_batch(s, batch=batch, mailer=mailer, settings=settings)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except Exception:
            # A crash *around* the send: the session, the bookkeeping write. The rows stay
            # `queued` and the next run carries them — with one honest caveat. If the crash
            # landed between the provider accepting the message and this commit, the reader
            # has the mail and will get it again in tomorrow's batch. The alternative — mark
            # `sent` before sending — turns that same window into a mail nobody ever gets,
            # and a duplicate alert is the better of the two failures.
            log.exception("sending alerts to user %s failed", batch.user_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"alert send aborted after {guard.consecutive} failures"
                log.error("notify: %s", result.abort_error)
                return result
            continue
        result.sent += outcome.sent
        result.failed += outcome.failed
        result.suppressed += outcome.suppressed
        if outcome.sent:
            result.mails_sent += 1
        if outcome.provider_failed:
            if await guard.failed():
                result.aborted = True
                result.abort_error = (
                    f"alert send aborted after {guard.consecutive} consecutive provider failures"
                )
                log.error("notify: %s", result.abort_error)
                return result
            continue
        guard.succeeded()

    log.info(
        "notify: %d alert mails sent (%d rows), %d failed, %d suppressed, %d lost",
        result.mails_sent,
        result.sent,
        result.failed,
        result.suppressed,
        result.failures,
    )
    return result


def alert_send_detail(result: AlertSendResult) -> str:
    """The send half of the notify run's `ingest_run.detail` line, appended to the decision
    pass's own (`notify_service.notify_detail`)."""
    line = (
        f"alerts: {result.mails_sent} mails to {result.users_considered} users, "
        f"{result.sent} sent, {result.failed} failed, {result.suppressed} suppressed, "
        f"{result.failures} lost"
    )
    if result.aborted:
        line += f"; {result.abort_error}"
    return line
