"""The digest sender: one mail per user on their cadence, carrying the `queued` digest rows the
decision pass wrote for them and — weekly — their slate (D-33).

`python -m upmovies.pipeline_run digest {daily|weekly}` runs this on two Coolify slots, one per
cadence. It is the digest counterpart of `alert_sender`: the decision pass
(`notify_service`) has already written a `digest` row per (user, event) for everything a
user's follows reach, so this pass never decides *whether* a user hears about an event — only
when, which is what `user_settings.digest_cadence` answers. A user with no settings row has
never opened the settings screen and holds the default, which is weekly (D-33), so the cadence
is read through a `COALESCE` rather than an inner join that would drop them.

**One mail per user per run, grouped the way the timeline is.** The mail is the user's
timeline since their last digest, so it groups the way the feed does: by publication day
(`event.created_at`, ADR-0016 — not `occurred_at`), newest day first, then by film within the
day, then the film's events in the order they happened. A backfill that lands as one tall day
on the feed lands as one tall day here too, deliberately.

**The weekly send is the "your slate" mail (D-33).** Before the timeline section it lists the
upcoming US dates — theatrical, digital and physical — for every film the user follows by
title in the next `SLATE_WINDOW_DAYS` (`app.follow_queries.title_follow_film_ids`, EF-14),
joined to `film_release_date` directly rather than to notification
rows: a date that has not *moved* produces
no event, and the slate's job is to say what is coming, not what changed. Each film's date per
release type is the governing one — the earliest row in the (film, US, type) subject, the same
collapse `public.service.get_calendar` and `catalog.headline_release` apply — so the slate
cannot name a date the calendar would not.

**A user with nothing queued and an empty slate gets no mail.** A digest with nothing to say
is worse than no digest, and it is the ordinary case for a quiet week.

**The access gate is re-read here, and it covers the slate** (D-37, D-39). The decision pass
already suppressed rows for unentitled and unverified users, so on the row side this is belt
and braces against a grant that lapsed after the rows were queued. The slate side is the case
that makes it necessary rather than merely consistent: the slate is built from the follows,
which D-40 keeps intact when a grant lapses, so without this check an unentitled user with
nothing queued would still receive a slate mail every week. The gate is one answer per user —
`entitled_user_clause()` AND `verified_user_clause()`, the two named rules every other pass
uses — and a user it refuses has their queued rows marked `suppressed` and gets no slate.

**Everything else follows `alert_sender`**: the copy is the summary the ledger holds now; a
row whose event was superseded or lost its summary is marked `failed` with the reason rather
than left to stall in the backlog; a provider refusal fails every row the mail carried and
counts toward the abort guard, so a dead provider stops the pass within `failure_threshold`
users and fails the run instead of converting the backlog into `failed` rows under a green
check.

**What this pass leaves alone.** A user whose cadence is `off` matches neither slot, so their
`digest` rows stay `queued` — the decision pass keeps writing them, because "do not mail me"
is a delivery preference and not a reason to stop deciding (D-40's shape: a preference change
back to `weekly` resumes with everything since). Nothing here prunes that backlog; a user who
turns the digest off for a year and back on will get a tall first digest.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx
from sqlalchemy import Date, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.follow_queries import title_follow_film_ids
from upmovies.app.models import (
    DEFAULT_DIGEST_CADENCE,
    Follow,
    Notification,
    User,
    UserSettings,
)
from upmovies.app.services.alert_sender import (
    BEAT_LABELS,
    EMAIL_CHANNEL,
    film_url,
    mark,
    poster_url,
    settings_url,
)
from upmovies.app.verification import verified_user_clause
from upmovies.catalog.models import Film, FilmReleaseDate
from upmovies.catalog.release_grade import PRIMARY_REGION, RELEASE_TYPE_BUCKETS
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
from upmovies.public.release import RELEASE_BUCKET_LABELS

log = logging.getLogger(__name__)

DIGEST_RUN_KIND = "digest"
"""This pass's `ingest_run.kind`, shared by both cadences — the detail line says which ran.
Unlike the notify pass it keeps no watermark: the backlog is the `queued` rows themselves."""

DIGEST_TEMPLATE = "digest"
"""The `mail/templates/` directory this pass renders. M7's contract names `digest` and `slate`
as two templates; they are one here because D-33 says the weekly send *is* the slate mail —
the slate is a section the weekly cadence turns on, not a second message."""

DIGEST_KIND = "digest"

DigestCadence = Literal["daily", "weekly"]
SEND_CADENCES: tuple[DigestCadence, ...] = ("daily", "weekly")
"""The cadences a slot can run. `off` is a `digest_cadence` value but not a slot: nothing is
sent for it, by definition."""

SLATE_WINDOW_DAYS = 30
"""How many dates the weekly slate covers: today and the 29 after it. "The next 30 days" is
thirty dates, not a 31-day span with both ends in."""

SLATE_RELEASE_TYPES: tuple[int, ...] = tuple(sorted(RELEASE_TYPE_BUCKETS))
"""TMDB release types the slate lists: the theatrical arc and the US home release — every
displayable bucket, US only, which is the region the home release is displayable in at all
(`catalog.release_grade`)."""

_SLATE_BUCKET_ORDER: tuple[str, ...] = ("wide", "limited", "digital", "physical")
"""Two rows sharing a date: the theatrical arc first, then the home release in the order it
happens — the calendar's rule (`public.service._CALENDAR_BUCKET_ORDER`)."""

DIGEST_BEAT_LABELS: dict[str, str] = {
    **BEAT_LABELS,
    "announced": "Announced",
    "casting": "Casting",
    "crew_attached": "Crew attached",
    "credit_removed": "Credit removed",
    "company_attached": "Studio attached",
    "company_removed": "Studio removed",
    "collection_attached": "Franchise attached",
    "collection_removed": "Franchise removed",
    "canceled": "Canceled",
    "production_start": "Production started",
    "production_wrap": "Production wrapped",
    "first_look": "First look",
}
"""What to call each event type in the digest. Every type the timeline shows, because the
digest is the timeline: the decision pass queues a digest line for every visible type, not just
D-32's three. The alert labels are spread in rather than restated so the two mails cannot name
one beat two ways. `digest_beat_label` falls back rather than raising for the reason
`alert_sender.beat_label` does: a new type must read plainly in one mail, not fail the batch."""

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def digest_beat_label(event_type: str) -> str:
    """The digest's name for an event type. Never raises — see `DIGEST_BEAT_LABELS`."""
    return DIGEST_BEAT_LABELS.get(event_type, "Update")


def day_heading(d: date) -> str:
    """The feed's day heading, spelled the same way (`public.service._day_heading`)."""
    return f"{_WEEKDAYS[d.weekday()]}, {_MONTHS[d.month - 1]} {d.day}, {d.year}"


def release_label(release_type: int) -> str:
    """The slate's name for a release type: the bucket's display label plus the word the film
    page's section heading supplies and a mail has to spell out."""
    return f"{RELEASE_BUCKET_LABELS[RELEASE_TYPE_BUCKETS[release_type]]} release"


@dataclass(frozen=True)
class DigestEvent:
    """One event line under a film."""

    notification_id: UUID
    beat: str
    summary: str


@dataclass(frozen=True)
class DigestFilm:
    """One film on one day, with the events it had that day."""

    title: str
    film_url: str
    poster_url: str | None
    events: tuple[DigestEvent, ...]


@dataclass(frozen=True)
class DigestDay:
    """One publication day of the user's timeline."""

    day: date
    films: tuple[DigestFilm, ...]


@dataclass(frozen=True)
class SlateItem:
    """One (film, release type) with a US date inside the slate window."""

    title: str
    release_label: str
    film_url: str
    poster_url: str | None


@dataclass(frozen=True)
class SlateDay:
    """One date on the slate and everything the user's followed films have on it."""

    day: date
    items: tuple[SlateItem, ...]


@dataclass(frozen=True)
class DigestRecipient:
    """One user this slot's cadence owes a look, and whether they may be mailed."""

    user_id: UUID
    email: str
    display_name: str
    deliverable: bool
    """Verified *and* entitled, re-read at send time — see the module docstring."""


@dataclass(frozen=True)
class DigestBatch:
    """Everything one user's digest would carry."""

    recipient: DigestRecipient
    days: tuple[DigestDay, ...]
    unsendable: tuple[tuple[UUID, str], ...]
    """`(notification id, why)` for rows this pass can never send — the event lost its summary
    or is no longer the published card — marked `failed` with the reason, as the alert sender
    does, so a permanently un-sendable row cannot stall silently in a backlog read nightly."""
    slate: tuple[SlateDay, ...] = ()

    @property
    def item_ids(self) -> list[UUID]:
        return [event.notification_id for d in self.days for f in d.films for event in f.events]

    @property
    def slate_count(self) -> int:
        return sum(len(d.items) for d in self.slate)

    @property
    def has_content(self) -> bool:
        return bool(self.item_ids) or bool(self.slate)


@dataclass
class DigestSendResult:
    """What one digest pass carried."""

    cadence: str
    users_considered: int = 0
    """Users on this cadence with something to look at — a `queued` digest row, or (weekly) a
    follow that might put a date on the slate. The working set, not the user table."""
    mails_sent: int = 0
    """Mails handed to the provider: inboxes touched, one per user. Reported beside `sent`
    because a weekly mail can carry a slate and no rows at all."""
    sent: int = 0
    failed: int = 0
    suppressed: int = 0
    """Rows marked `suppressed` because the gate answered no at send time."""
    users_gated: int = 0
    """Users the gate refused, whether or not they held rows — the slate side has no row to
    mark, so without this number a lapsed subscriber with an empty queue would leave no trace
    of having been considered and refused (D-39)."""
    slate_dates: int = 0
    """Slate entries — (film, release type) dates — across every mail sent."""
    failures: int = 0
    """Batches lost to a crash around the send, as opposed to a provider refusing a mail. Their
    rows stay `queued`; see `alert_sender.AlertSendResult.failures`."""
    aborted: bool = False
    abort_error: str | None = None


@dataclass(frozen=True)
class DigestOutcome:
    """What one batch did: rows changed, whether a mail went out, and whether the provider is
    the reason it did not."""

    sent: int = 0
    failed: int = 0
    suppressed: int = 0
    mailed: bool = False
    slate_dates: int = 0
    gated: bool = False
    provider_failed: bool = False


async def load_recipients(
    session: AsyncSession, *, cadence: DigestCadence
) -> list[DigestRecipient]:
    """Every user on `cadence` with something this slot might mail, oldest account first.

    The cadence is `COALESCE`d over an outer join because the settings row is created lazily
    (`app.models.UserSettings`): a user who has never opened the settings screen has no row and
    holds the default, and an inner join would silently drop every one of them from the weekly
    digest — which is the digest most users get.

    The gate is read as a column rather than a filter, for the reason the alert sender gives:
    a user it refuses is owed `suppressed` rows, not silence. The `EXISTS` terms keep the pass
    proportional to what is owed rather than to signups: on the daily cadence only a queued row
    puts a user in the set; weekly adds anyone with a **follow**, because the slate is computed
    from the follow graph (M8) and needs no row at all. A follow that covers nothing dated
    costs one empty slate query and no mail — "nothing queued and an empty slate gets no mail"
    already covers it."""
    queued_digest = exists().where(
        Notification.user_id == User.id,
        Notification.kind == DIGEST_KIND,
        Notification.channel == EMAIL_CHANNEL,
        Notification.status == "queued",
    )
    owed = queued_digest
    if cadence == "weekly":
        owed = or_(queued_digest, exists().where(Follow.user_id == User.id))
    rows = await session.execute(
        select(
            User.id,
            User.email,
            User.display_name,
            and_(entitled_user_clause(), verified_user_clause()).label("deliverable"),
        )
        .outerjoin(UserSettings, UserSettings.user_id == User.id)
        .where(
            func.coalesce(UserSettings.digest_cadence, DEFAULT_DIGEST_CADENCE) == cadence,
            owed,
        )
        .order_by(User.created_at, User.id)
    )
    return [
        DigestRecipient(
            user_id=row.id,
            email=row.email,
            display_name=row.display_name,
            deliverable=row.deliverable,
        )
        for row in rows
    ]


@dataclass
class _FilmDay:
    title: str
    film_url: str
    poster_url: str | None
    events: list[tuple[datetime, datetime, UUID, DigestEvent]] = field(default_factory=list)


async def load_timeline(
    session: AsyncSession, *, user_id: UUID, settings: Settings
) -> tuple[tuple[DigestDay, ...], tuple[tuple[UUID, str], ...]]:
    """This user's `queued` digest rows as timeline days, plus the rows that can never be sent.

    The join is the mail — film title, poster and ref, the event's type and summary — and, as
    in the alert sender, the event's status and summary are re-read rather than trusted from
    the queue, because the queue outlives the run that wrote it. `EventSummary` is the one
    outer join so a missing summary comes back as a row to fail rather than a row that quietly
    disappears.

    Grouping happens here rather than in SQL because the shape is nested three deep and the
    rows are one user's, not the ledger's."""
    rows = await session.execute(
        select(
            Notification.id,
            Event.id.label("event_id"),
            Event.event_type,
            Event.status.label("event_status"),
            Event.created_at,
            Event.occurred_at,
            EventSummary.summary,
            Film.id.label("film_id"),
            Film.tmdb_id,
            Film.title,
            Film.poster_path,
        )
        .join(Event, Event.id == Notification.event_id)
        .join(Film, Film.id == Event.film_id)
        .outerjoin(EventSummary, EventSummary.event_id == Event.id)
        .where(
            Notification.user_id == user_id,
            Notification.kind == DIGEST_KIND,
            Notification.channel == EMAIL_CHANNEL,
            Notification.status == "queued",
        )
    )
    unsendable: list[tuple[UUID, str]] = []
    by_day: dict[date, dict[UUID, _FilmDay]] = {}
    for row in rows:
        if row.event_status != "published":
            unsendable.append((row.id, "the event is no longer published"))
            continue
        if row.summary is None:
            unsendable.append((row.id, "the event has no summary"))
            continue
        # The publication day, in UTC, exactly as the feed keys it (ADR-0016).
        day = row.created_at.astimezone(UTC).date()
        films = by_day.setdefault(day, {})
        film = films.get(row.film_id)
        if film is None:
            film = films[row.film_id] = _FilmDay(
                title=row.title,
                film_url=film_url(row.tmdb_id, row.title, settings.public_base_url),
                poster_url=poster_url(row.poster_path, settings.tmdb_image_base),
            )
        film.events.append(
            (
                row.occurred_at,
                row.created_at,
                row.event_id,
                DigestEvent(
                    notification_id=row.id,
                    beat=digest_beat_label(row.event_type),
                    summary=row.summary,
                ),
            )
        )
    days = tuple(
        DigestDay(
            day=day,
            films=tuple(
                DigestFilm(
                    title=film.title,
                    film_url=film.film_url,
                    poster_url=film.poster_url,
                    # In the order the news happened, as the feed orders a film-day's events.
                    events=tuple(event for _, _, _, event in sorted(film.events, key=_event_key)),
                )
                for film in sorted(films.values(), key=lambda f: f.title.casefold())
            ),
        )
        # Newest day first, as the feed pages.
        for day, films in sorted(by_day.items(), key=lambda item: item[0], reverse=True)
    )
    return days, tuple(unsendable)


def _event_key(
    entry: tuple[datetime, datetime, UUID, DigestEvent],
) -> tuple[datetime, datetime, str]:
    occurred_at, created_at, event_id, _ = entry
    return occurred_at, created_at, str(event_id)


async def load_slate(
    session: AsyncSession, *, user_id: UUID, today: date, settings: Settings
) -> tuple[SlateDay, ...]:
    """The upcoming US dates for the films this user follows, soonest first (D-33).

    The set is `follow_queries.title_follow_film_ids` — the films they asked for by name, and
    only those (EF-14). A followed director contributes nothing: an entity follow delivers that
    entity's attachment cards, not a place on a date list (EF-3). The same set the my-films
    calendar and the `.ics` feed read, so the three cannot disagree about what is coming.

    One governing date per (film, release type): the earliest `film_release_date` row in the
    subject, cast to a UTC calendar date — the same collapse `public.service.get_calendar`
    makes, restricted to `PRIMARY_REGION` because that is the only region every displayable
    bucket is displayable in (`catalog.release_grade`). The window is `SLATE_WINDOW_DAYS`
    dates starting today: a date that is today is still a date to know about, and the day
    the count runs out is the first one left off.

    The calendar's popularity, runtime and adult cuts are deliberately absent. Those keep noise
    off a public listing; a film the user followed by name is not noise to them. A film with no
    slug is skipped for the reason the decision pass skips it — no page to link.
    """
    governing = (
        select(
            FilmReleaseDate.film_id.label("film_id"),
            FilmReleaseDate.release_type.label("release_type"),
            func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)).label(
                "governing_date"
            ),
        )
        .where(
            FilmReleaseDate.film_id.in_(title_follow_film_ids(user_id)),
            FilmReleaseDate.iso_3166_1 == PRIMARY_REGION,
            FilmReleaseDate.release_type.in_(SLATE_RELEASE_TYPES),
        )
        .group_by(FilmReleaseDate.film_id, FilmReleaseDate.release_type)
        .cte("governing")
    )
    rows = (
        await session.execute(
            select(
                governing.c.governing_date,
                governing.c.release_type,
                Film.tmdb_id,
                Film.title,
                Film.poster_path,
            )
            .select_from(governing)
            .join(Film, Film.id == governing.c.film_id)
            .where(
                governing.c.governing_date >= today,
                governing.c.governing_date < today + timedelta(days=SLATE_WINDOW_DAYS),
                Film.slug.is_not(None),
            )
        )
    ).all()
    ordered = sorted(
        rows,
        key=lambda r: (
            r.governing_date,
            _SLATE_BUCKET_ORDER.index(RELEASE_TYPE_BUCKETS[r.release_type]),
            r.title.casefold(),
            r.tmdb_id,
        ),
    )
    by_day: dict[date, list[SlateItem]] = {}
    for row in ordered:
        by_day.setdefault(row.governing_date, []).append(
            SlateItem(
                title=row.title,
                release_label=release_label(row.release_type),
                film_url=film_url(row.tmdb_id, row.title, settings.public_base_url),
                poster_url=poster_url(row.poster_path, settings.tmdb_image_base),
            )
        )
    return tuple(SlateDay(day=day, items=tuple(items)) for day, items in by_day.items())


async def load_batch(
    session: AsyncSession,
    *,
    recipient: DigestRecipient,
    cadence: DigestCadence,
    today: date,
    settings: Settings,
) -> DigestBatch:
    """Everything one user's digest would carry on this cadence.

    The slate is loaded only for a weekly send *and* only for a user the gate admits: for a
    refused user the answer is already "no mail", and reading their follows would be work in
    service of a section that must not be sent (D-39)."""
    days, unsendable = await load_timeline(session, user_id=recipient.user_id, settings=settings)
    slate: tuple[SlateDay, ...] = ()
    if cadence == "weekly" and recipient.deliverable:
        slate = await load_slate(session, user_id=recipient.user_id, today=today, settings=settings)
    return DigestBatch(recipient=recipient, days=days, unsendable=unsendable, slate=slate)


def digest_context(
    batch: DigestBatch, *, cadence: DigestCadence, settings: Settings
) -> dict[str, object]:
    """The `digest` template's context for one batch. The subject line is the template's own
    business, as it is for the alert; the counts it chooses between are supplied here."""
    return {
        "product_name": settings.product_name,
        "display_name": batch.recipient.display_name,
        "settings_url": settings_url(settings.public_base_url),
        "cadence": cadence,
        "slate_window_days": SLATE_WINDOW_DAYS,
        "slate": [
            {
                "heading": day_heading(d.day),
                # `entries`, not `items`: Jinja resolves `day.items` to the dict method.
                "entries": [
                    {
                        "title": item.title,
                        "release_label": item.release_label,
                        "film_url": item.film_url,
                        "poster_url": item.poster_url,
                    }
                    for item in d.items
                ],
            }
            for d in batch.slate
        ],
        "days": [
            {
                "heading": day_heading(d.day),
                "films": [
                    {
                        "title": f.title,
                        "film_url": f.film_url,
                        "poster_url": f.poster_url,
                        "events": [{"beat": e.beat, "summary": e.summary} for e in f.events],
                    }
                    for f in d.films
                ],
            }
            for d in batch.days
        ],
        "update_count": len(batch.item_ids),
        "slate_count": batch.slate_count,
    }


async def send_batch(
    session: AsyncSession,
    *,
    batch: DigestBatch,
    cadence: DigestCadence,
    mailer: Mailer,
    settings: Settings,
) -> DigestOutcome:
    """Send one user's digest and record every row's outcome. The caller commits.

    The gate is answered first and is the whole answer for a refused user: every row they hold
    is `suppressed`, including the ones with no copy, and the slate — already withheld by
    `load_batch` — is not sent. Then the un-sendable rows are failed by reason, and only then is
    there a mail to send, and only if there is something to put in it. The provider's errors are
    the set `alert_sender.send_batch` catches, for its reasons."""
    unsendable_ids = [row_id for row_id, _ in batch.unsendable]
    item_ids = batch.item_ids
    if not batch.recipient.deliverable:
        await mark(session, unsendable_ids + item_ids, status="suppressed")
        return DigestOutcome(suppressed=len(unsendable_ids) + len(item_ids), gated=True)
    by_reason: dict[str, list[UUID]] = {}
    for row_id, why in batch.unsendable:
        by_reason.setdefault(why, []).append(row_id)
    for why, row_ids in by_reason.items():
        await mark(session, row_ids, status="failed", error=why)
    failed = len(unsendable_ids)
    if not batch.has_content:
        return DigestOutcome(failed=failed)
    try:
        await mailer.send(
            to=batch.recipient.email,
            template=DIGEST_TEMPLATE,
            context=digest_context(batch, cadence=cadence, settings=settings),
        )
    except (MailError, MailConfigurationError, MissingCredentialError, httpx.HTTPError) as exc:
        log.exception("digest mail to user_id=%s failed", batch.recipient.user_id)
        await mark(session, item_ids, status="failed", error=f"{type(exc).__name__}: {exc}")
        return DigestOutcome(failed=failed + len(item_ids), provider_failed=True)
    await mark(session, item_ids, status="sent", sent_at=datetime.now(UTC))
    return DigestOutcome(
        sent=len(item_ids), failed=failed, mailed=True, slate_dates=batch.slate_count
    )


async def send_digests(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    cadence: DigestCadence,
    today: date,
    mailer: Mailer,
    settings: Settings,
    failure_threshold: int = 10,
) -> DigestSendResult:
    """Mail every user on `cadence` their digest, and record each row's outcome.

    The pipeline conventions the other passes state: one session per user so a failure never
    rolls back the others, `record_progress` against the run id, abort after N consecutive
    failures, and **no** `finalize_run` — the status and detail line belong to
    `pipeline_run.run_digest_stage`. The abort guard counts provider refusals for the reason
    `alert_sender.send_queued_alerts` gives: a provider is one shared dependency, and a run of
    refusals is one outage that must fail the run rather than convert the backlog.
    """
    if cadence not in SEND_CADENCES:
        raise ValueError(f"digest cadence must be one of {SEND_CADENCES}, not {cadence!r}")
    result = DigestSendResult(cadence=cadence)
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        recipients = await load_recipients(s, cadence=cadence)
    result.users_considered = len(recipients)
    log.info("digest %s: %d users to consider", cadence, result.users_considered)

    for recipient in recipients:
        await heartbeat.tick()
        try:
            async with owned_session(session_factory) as s:
                batch = await load_batch(
                    s, recipient=recipient, cadence=cadence, today=today, settings=settings
                )
                outcome = await send_batch(
                    s, batch=batch, cadence=cadence, mailer=mailer, settings=settings
                )
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except Exception:
            # A crash around the send leaves the rows `queued` for the next slot — with the
            # duplicate-over-silence caveat `alert_sender.send_queued_alerts` states.
            log.exception("sending the %s digest to user %s failed", cadence, recipient.user_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"digest send aborted after {guard.consecutive} failures"
                log.error("digest %s: %s", cadence, result.abort_error)
                return result
            continue
        result.sent += outcome.sent
        result.failed += outcome.failed
        result.suppressed += outcome.suppressed
        result.slate_dates += outcome.slate_dates
        if outcome.mailed:
            result.mails_sent += 1
        if outcome.gated:
            result.users_gated += 1
        if outcome.provider_failed:
            if await guard.failed():
                result.aborted = True
                result.abort_error = (
                    f"digest send aborted after {guard.consecutive} consecutive provider failures"
                )
                log.error("digest %s: %s", cadence, result.abort_error)
                return result
            continue
        guard.succeeded()

    log.info(
        "digest %s: %d mails sent (%d rows, %d slate dates), %d failed, %d suppressed, %d lost",
        cadence,
        result.mails_sent,
        result.sent,
        result.slate_dates,
        result.failed,
        result.suppressed,
        result.failures,
    )
    return result


def digest_detail(result: DigestSendResult) -> str:
    """The run's `ingest_run.detail` line. `gated` sits beside `suppressed` because the two
    answer different questions: rows the gate refused, and users it refused — a weekly user
    with an empty queue and a lapsed grant is one of the second and none of the first."""
    line = (
        f"digest {result.cadence}: {result.mails_sent} mails to {result.users_considered} users, "
        f"{result.sent} sent, {result.failed} failed, {result.suppressed} suppressed, "
        f"{result.users_gated} gated, {result.slate_dates} slate dates, {result.failures} lost"
    )
    if result.aborted:
        line += f"; {result.abort_error}"
    return line
