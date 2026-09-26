"""The digest sender: one mail per user on their cadence, carrying the `queued` digest rows the
decision pass wrote for them and — weekly, and daily on the slate day — their slate (D-33,
DC-2).

`python -m upmovies.pipeline_run digest {daily|weekly}` runs this on two Coolify slots, one per
cadence. It is the product's only delivery (ADR-0021): the decision pass
(`notify_service`) has already written a `digest` row per (user, event) for everything a
user's follows reach, so this pass never decides *whether* a user hears about an event — only
when, which is what `user_settings.digest_cadence` answers. A user with no settings row has
never opened the settings screen and holds the default, which is weekly (D-33), so the cadence
is read through a `COALESCE` rather than an inner join that would drop them.

**One mail per user per run, one film entry per film (DC-3).** The mail is the user's
timeline since their last digest, but it does not repeat the feed's day grouping: a beat line
is only legible under its film's header (catalog summaries never name the film), so each film
appears once, as a **film entry** — a header (poster, title, the feed's parenthetical, a status
line), the follows that reached it ("Following:", DC-6), and its beats in the order they were
published (`event.created_at`, ADR-0016; `occurred_at` breaks ties). Entries are ranked by
their most significant beat on the film arc (`public.arc`), then title, then `tmdb_id`, and the
top one — the **lead film** — names the subject. Every beat line carries its publication date,
an **Unconfirmed** marker when rumored, and its first source (DC-5). At most
`DIGEST_MAX_ENTRIES` entries are rendered; the rest are one line pointing at the timeline,
and every row is still marked `sent` (DC-8).

**One render path (D-1460.1).** `render_batch` turns a batch into an `Envelope`; `send_batch`
hands that to `Mailer.deliver`, and `render_digest` — what the admin preview and test-send
call — returns it without sending. `digest_context` is the only place the template's dict is
built, so the three cannot disagree about what the mail says.

**The weekly send is the "your slate" mail (D-33), and so is the daily one on the slate day
(DC-2).** The weekly always carries the slate; the daily carries it only when the run's `today`
falls on `SLATE_WEEKDAY`, so a daily reader sees upcoming dates once a week, on the day the
weekly readers do. Before the timeline section it lists the upcoming US dates — theatrical,
digital and physical — for every film the user follows by
title in the next `SLATE_WINDOW_DAYS` (`app.follow_queries.title_follow_film_ids`, EF-14),
joined to `film_release_date` directly rather than to notification
rows: a date that has not *moved* produces
no event, and the slate's job is to say what is coming, not what changed. Each film's date per
release type is the governing one — the earliest row in the (film, US, type) subject, the same
collapse `public.service.get_calendar` and `catalog.headline_release` apply — so the slate
cannot name a date the calendar would not. A date **set or moved since the previous slate
day** carries a `new` or `moved` marker (DC-9), read from the release-date card that set or
moved it — see `load_slate_markers`.

**A user with nothing queued and an empty slate gets no mail.** A digest with nothing to say
is worse than no digest, and it is the ordinary case for a quiet week. The converse holds on
both cadences whenever the slate is in: nothing queued and a non-empty slate is a mail.

**The access gate is re-read here, and it covers the slate** (D-37, D-39). The decision pass
already suppressed rows for unentitled and unverified users, so on the row side this is belt
and braces against a grant that lapsed after the rows were queued. The slate side is the case
that makes it necessary rather than merely consistent: the slate is built from the follows,
which D-40 keeps intact when a grant lapses, so without this check an unentitled user with
nothing queued would still receive a slate mail every week. The gate is one answer per user —
`entitled_user_clause()` AND `verified_user_clause()`, the two named rules every other pass
uses — and a user it refuses has their queued rows marked `suppressed` and gets no slate.

**The queue outlives the run that wrote it, and everything else follows from that**: the copy
is the summary the ledger holds now; a row whose event was superseded or lost its summary is
marked `failed` with the reason rather than left to stall in the backlog; a provider refusal
fails every row the mail carried and counts toward the abort guard, so a dead provider stops
the pass within `failure_threshold` users and fails the run instead of converting the backlog
into `failed` rows under a green check. `failed` is terminal — neither this pass nor the
decision pass's `ON CONFLICT DO NOTHING` reconsiders one, so recovering a row means an operator
re-queuing it by hand.

**Every digest carries a one-click unsubscribe (DC-10).** `List-Unsubscribe` names
`{API_BASE_URL}/digest/unsubscribe/{token}` and `List-Unsubscribe-Post` makes it RFC 8058 one
click; the footer's "unsubscribe" links the same URL. The token lives on the settings row, which
a weekly reader who never opened their settings does not have — so a user this pass is about to
mail gets their row created here (`ensure_unsubscribe_token`), and only that user: the gate has
passed and there is something to send. `render_digest` writes nothing, and renders a rowless
user's mail without the header.

**What this pass leaves alone.** A user whose cadence is `off` matches neither slot, so their
`digest` rows stay `queued` — the decision pass keeps writing them, because "do not mail me"
is a delivery preference and not a reason to stop deciding (D-40's shape: a preference change
back to `weekly` resumes with everything since). Nothing here prunes that backlog; a user who
turns the digest off for a year and back on will get a tall first digest.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID

import httpx
from sqlalchemy import Date, Row, and_, cast, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app import tokens
from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.follow_queries import follow_attribution_pairs, title_follow_film_ids
from upmovies.app.models import (
    DEFAULT_DIGEST_CADENCE,
    Follow,
    Notification,
    User,
    UserSettings,
)
from upmovies.app.repos import user_settings_repo
from upmovies.app.services.notify_service import EMAIL_CHANNEL
from upmovies.app.verification import verified_user_clause
from upmovies.catalog.headline_release import HeadlineRelease, headline_releases
from upmovies.catalog.models import (
    Collection,
    Film,
    FilmReleaseDate,
    FilmReleaseDateChange,
    Person,
    ProductionCompany,
)
from upmovies.catalog.ref import collection_ref, company_ref, film_ref, person_ref
from upmovies.catalog.release_grade import PRIMARY_REGION, RELEASE_TYPE_BUCKETS
from upmovies.config import WEEKDAYS, Settings
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.release_date_history import RELEASE_DATE_MOVED, RELEASE_DATE_SET
from upmovies.mail import (
    Envelope,
    MailConfigurationError,
    Mailer,
    MailError,
    MessageId,
    MissingCredentialError,
    render,
)
from upmovies.news.models import Event, EventSummary
from upmovies.public.arc import derive_arc_stage, event_stage_rank, most_significant_event_type
from upmovies.public.release import RELEASE_BUCKET_LABELS
from upmovies.public.service import (
    _directors_for_films,
    _production_countries_for_films,
    _release_year,
    _sources_by_event,
)
from upmovies.public.sources import cap_sources, outlet_label, source_url

log = logging.getLogger(__name__)

DIGEST_RUN_KIND = "digest"
"""This pass's `ingest_run.kind`, shared by both cadences — the detail line says which ran.
Unlike the notify pass it keeps no watermark: the backlog is the `queued` rows themselves."""

DIGEST_TEMPLATE = "digest"
"""The `mail/templates/` directory this pass renders. M7's contract names `digest` and `slate`
as two templates; they are one here because D-33 says the weekly send *is* the slate mail —
the slate is a section the weekly cadence turns on, not a second message."""

DIGEST_KIND = "digest"

POSTER_SIZE = "w154"
"""TMDB's small poster width, and a deliberate floor. A mail is read on a phone over a mobile
connection and its images are fetched before the reader has decided they want them, so the
poster is a thumbnail beside the copy rather than the artwork it is on the film page."""

LEAD_POSTER_SIZE = "w185"
"""The poster width for the lead film's card, shown at 92px (DC-14). `w154` is under two
device pixels per CSS pixel at that size, so the one poster a mail leads with would be the one
that renders soft on a phone."""

JUSTWATCH_EVENT_TYPE = "now_available"
"""The beat whose data is JustWatch's, via TMDB's watch-provider endpoint — the condition on
that data is a visible credit wherever it is shown (DC-17)."""

DigestCadence = Literal["daily", "weekly"]
SEND_CADENCES: tuple[DigestCadence, ...] = ("daily", "weekly")
"""The cadences a slot can run. `off` is a `digest_cadence` value but not a slot: nothing is
sent for it, by definition."""

SLATE_WINDOW_DAYS = 30
"""How many dates the slate covers: today and the 29 after it. "The next 30 days" is
thirty dates, not a 31-day span with both ends in."""

DIGEST_MAX_ENTRIES = 20
"""How many film entries one digest renders (DC-8). The rest are one line pointing at the
timeline, where they already are — and their rows are still marked `sent`, because the
timeline is where the rest lives and nothing is re-queued."""

SLATE_RELEASE_TYPES: tuple[int, ...] = tuple(sorted(RELEASE_TYPE_BUCKETS))
"""TMDB release types the slate lists: the theatrical arc and the US home release — every
displayable bucket, US only, which is the region the home release is displayable in at all
(`catalog.release_grade`)."""

SLATE_MARKER_DAYS = 7
"""How far back a slate row looks for the release-date card that set or moved it (DC-9): the
run's UTC day and the six before it. Calendar days rather than a rolling `now - 7d` because the
run knows its day and not its slot's time. That is "since the previous slate day" as long as
the sweep that cards a date runs ahead of the digest slot, which it is scheduled to: the
previous slate day's cards were then in the previous slate. A card created on that day
*after* its digest slot — a late or re-run chain — falls in neither window and is never
marked. Accepted: a missing marker costs a reader a hint, while a doubled one would call the
same move news two weeks running."""

SlateMarker = Literal["new", "moved"]

_SLATE_BUCKET_ORDER: tuple[str, ...] = ("wide", "limited", "digital", "physical")
"""Two rows sharing a date: the theatrical arc first, then the home release in the order it
happens — the calendar's rule (`public.service._CALENDAR_BUCKET_ORDER`)."""

DIGEST_BEAT_LABELS: dict[str, str] = {
    "release_date": "Release date",
    "now_available": "Now available",
    "trailer": "New trailer",
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
digest is the timeline: the decision pass queues a digest line for every visible type.
`digest_beat_label` falls back rather than raising, for the reason `_render_status` does in
`synthesize.deterministic`: a new type must read plainly in one mail, not fail the batch."""

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


def poster_url(poster_path: str | None, image_base: str, *, size: str = POSTER_SIZE) -> str | None:
    """The absolute URL for a poster path at a TMDB `size`, or None when the film has no
    poster.

    Absolute because a mail has no page to resolve a relative path against. None rather than a
    placeholder image: the template drops the poster cell entirely, which reads better than a
    grey box and costs the reader one fewer image fetch."""
    if not poster_path:
        return None
    return f"{image_base.rstrip('/')}/{size}{poster_path}"


def film_url(tmdb_id: int, title: str, base_url: str) -> str:
    """The film's public page — the same `/film/{ref}` the sitemap emits, built from the same
    `film_ref`, so a link in a mail cannot address a film differently from a link on the site
    (and cannot land on the 301 a bare id would)."""
    return f"{base_url.rstrip('/')}/film/{film_ref(tmdb_id, title)}"


def settings_url(base_url: str) -> str:
    """Where the mail's settings link points: the reader's own settings page, which is where
    `digest_cadence` lives (D-33). The one-click unsubscribe (DC-10) sits beside it rather than
    replacing it — a reader who wants a different cadence is not asking to leave."""
    return f"{base_url.rstrip('/')}/settings"


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
    the only route back — see the module docstring.)"""
    if not ids:
        return
    await session.execute(
        update(Notification)
        .where(Notification.id.in_(ids))
        .values(status=status, sent_at=sent_at, error=error)
    )


def day_heading(d: date) -> str:
    """The feed's day heading, spelled the same way (`public.service._day_heading`)."""
    return f"{_WEEKDAYS[d.weekday()]}, {_MONTHS[d.month - 1]} {d.day}, {d.year}"


def carries_slate(cadence: DigestCadence, today: date, settings: Settings) -> bool:
    """Whether this cadence's mail carries the slate on `today` (DC-2): the weekly always, the
    daily only on `SLATE_WEEKDAY`. The weekly is not held to the weekday because the repo
    cannot see the Coolify schedule — the setting documents the day that slot must run on."""
    if cadence == "weekly":
        return True
    return today.weekday() == WEEKDAYS.index(settings.slate_weekday)


def release_label(release_type: int) -> str:
    """The slate's name for a release type: the bucket's display label plus the word the film
    page's section heading supplies and a mail has to spell out."""
    return f"{RELEASE_BUCKET_LABELS[RELEASE_TYPE_BUCKETS[release_type]]} release"


ARC_STAGE_LABELS: dict[str, str] = {
    "announced": "Announced",
    "shooting": "Shooting",
    "wrapped": "Wrapped",
    "released": "Released",
}
"""The frontend's `components/film/labels.ts::ARC_STAGE_LABELS`, word for word (D-1460.3): the
parenthetical falls back to one of these, and it has to read as the feed row reads."""

_COUNTRY_CAP = 3
_DIRECTOR_CAP = 2
"""`filmParenthetical`'s caps (`lib/format.ts`): the feed row has the width for three
countries and two directors, and the mail's header is that row."""

_FOLLOWING_ROUTES: dict[str, str] = {
    "person": "person",
    "company": "studio",
    "franchise": "franchise",
}
"""Follow `entity_type` → the frontend route segment its page lives under. The follow graph's
words (`company`, `franchise`) are not the reader's (EF-19)."""

_FOLLOWING_ORDER: tuple[str, ...] = ("person", "company", "franchise", "title")


def arc_stage_label(stage: str) -> str:
    """The reader's word for an arc stage; an unknown one reads as "Announced", as the
    frontend's `arcStageLabel` has it."""
    return ARC_STAGE_LABELS.get(stage, ARC_STAGE_LABELS["announced"])


def _join_capped(values: Sequence[str], cap: int) -> str | None:
    if not values:
        return None
    if len(values) <= cap:
        return "/".join(values)
    return f"{'/'.join(values[:cap])} +{len(values) - cap}"


def film_parenthetical(
    *,
    production_countries: Sequence[str],
    directors: Sequence[str],
    release_year: int | None,
    arc_stage: str,
) -> str:
    """The feed row's title parenthetical, without its parentheses — spelled exactly as the
    frontend's `filmParenthetical` spells it (DC-4, NEU-1215), so a reader who sees a film in
    the mail and on the feed reads the same words.

    Countries then `Dir:` then year, joined with ", "; multi-value elements join with "/" so
    the comma always means "next element", capped with " +N". The arc-stage label only when
    all three are absent. Tested against the frontend's own fixtures."""
    parts: list[str] = []
    countries = _join_capped(production_countries, _COUNTRY_CAP)
    if countries:
        parts.append(countries)
    names = _join_capped(directors, _DIRECTOR_CAP)
    if names:
        parts.append(f"Dir: {names}")
    if release_year is not None:
        parts.append(str(release_year))
    if not parts:
        return arc_stage_label(arc_stage)
    return ", ".join(parts)


def long_date(d: date) -> str:
    """ "14 August 2026" — the status line's date: no weekday, no ordinal, no zero-pad."""
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def short_date(d: date, *, today: date) -> str:
    """ "22 Sep" — a beat's publication day (DC-5, DC-12). The year is appended only when it is
    not the run's year, which a tall digest after months of `off` can span. `today` is the
    run's, never the wall clock's, so a test's answer does not depend on when it runs."""
    day = f"{d.day} {_MONTHS[d.month - 1][:3]}"
    return day if d.year == today.year else f"{day} {d.year}"


def status_line(release: HeadlineRelease | None, arc_stage: str) -> str:
    """The film header's one status line (D-1460.2): the headline release the film page leads
    with, or the arc stage when there is none.

    `headline_releases` is theatrical-only, so the dated forms are a theatrical bucket and a
    date, tense-free ("Wide release · 14 August 2026") — or, for the `primary` fallback that
    belongs to no bucket, the date marked unconfirmed, as the feed row marks it."""
    if release is None:
        return arc_stage_label(arc_stage)
    if release.bucket is None:  # `kind == "primary"`, by `HeadlineRelease`'s own contract
        return f"{long_date(release.date)} (unconfirmed)"
    return f"{RELEASE_BUCKET_LABELS[release.bucket]} release · {long_date(release.date)}"


@dataclass(frozen=True)
class DigestSource:
    """Where a beat came from, as one line under it (DC-5)."""

    name: str
    """The outlet's label for a story card; "TMDB" for a catalog card."""
    url: str | None
    """The story's link; None for a catalog card, which reads "via TMDB", unlinked."""


CATALOG_SOURCE = DigestSource(name="TMDB", url=None)


@dataclass(frozen=True)
class DigestBeat:
    """One beat line in a film entry."""

    notification_id: UUID
    event_id: UUID
    event_type: str
    created_at: datetime
    occurred_at: datetime
    confidence: str
    """`confirmed` or `rumored`; a rumored beat carries the Unconfirmed marker."""
    summary: str
    source: DigestSource | None
    """None for a story card with no story row left — no source line, and no invented one."""

    @property
    def day(self) -> date:
        """The publication day, in UTC, exactly as the feed keys it (ADR-0016)."""
        return self.created_at.astimezone(UTC).date()

    @property
    def label(self) -> str:
        return digest_beat_label(self.event_type)


@dataclass(frozen=True)
class DigestFollowing:
    """One follow that reached a beat in an entry."""

    entity_type: str
    """`person`, `company`, `franchise` or `title` — the follow graph's words."""
    name: str
    url: str | None
    """The entity's page; None for the title row, whose link is the entry's own."""


@dataclass(frozen=True)
class DigestFilm:
    """The film an entry is about, resolved to what the header links and shows."""

    film_id: UUID
    tmdb_id: int
    title: str
    film_url: str
    poster_url: str | None
    """The compact row's poster (`w154`, shown at 62px)."""
    lead_poster_url: str | None
    """The lead card's poster (`w185`, shown at 92px, DC-14). Built for every film because
    which film leads is decided by ranking the whole batch, after the lookups."""


@dataclass(frozen=True)
class DigestHeader:
    """The two lines of a film entry's header that are computed rather than looked up."""

    parenthetical: str
    status: str


@dataclass(frozen=True)
class DigestEntry:
    """One **film entry**: a film, its header, the follows that reached it and its beats in
    publication order (DC-3)."""

    film: DigestFilm
    header: DigestHeader
    following: tuple[DigestFollowing, ...]
    """Every attribution row, the title row included — `entity_following` is what the mail
    shows; the title row is there so a preview can state the entry's full reach (DC-6)."""
    beats: tuple[DigestBeat, ...]

    @property
    def lead_type(self) -> str:
        """The entry's most significant beat type on the film arc."""
        return most_significant_event_type(beat.event_type for beat in self.beats)

    @property
    def lead_label(self) -> str:
        return digest_beat_label(self.lead_type)

    @property
    def rank_key(self) -> tuple[int, str, int]:
        """Most significant lead beat first, then title, then `tmdb_id` (DC-3)."""
        return (-event_stage_rank(self.lead_type), self.film.title.casefold(), self.film.tmdb_id)

    @property
    def entity_following(self) -> tuple[DigestFollowing, ...]:
        """The rows the "Following:" line names: entity follows only. A title follow is the
        reader having asked for the film by name, which needs no telling (DC-6)."""
        return tuple(f for f in self.following if f.entity_type != "title")

    @property
    def credits_justwatch(self) -> bool:
        return any(beat.event_type == JUSTWATCH_EVENT_TYPE for beat in self.beats)


def rank_entries(entries: Iterable[DigestEntry]) -> tuple[DigestEntry, ...]:
    """Film entries in mail order: by `DigestEntry.rank_key` (DC-3). The top one is the lead
    film, which names the subject."""
    return tuple(sorted(entries, key=lambda entry: entry.rank_key))


def beat_order_key(beat: DigestBeat) -> tuple[datetime, datetime, str]:
    """A film entry's beats in **publication** order — `created_at`, then `occurred_at`, then
    the event id so two beats published together still sort the same way twice (DC-3)."""
    return beat.created_at, beat.occurred_at, str(beat.event_id)


@dataclass(frozen=True)
class SlateItem:
    """One (film, release type) with a US date inside the slate window."""

    title: str
    release_label: str
    film_url: str
    poster_url: str | None
    marker: SlateMarker | None = None
    """`new` or `moved` when the date was set or moved since the previous slate day (DC-9);
    None for a date that has not changed, which carries nothing."""


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
    unsubscribe_token: str | None = None
    """The settings row's, or None for a user who has no row yet — `send_batch` creates one
    before it mails them (DC-10)."""


@dataclass(frozen=True)
class DigestBatch:
    """Everything one user's digest would carry."""

    recipient: DigestRecipient
    entries: tuple[DigestEntry, ...]
    """Ranked and **uncapped**: the subject's count and `item_ids` both need the whole batch,
    and only the rendering stops at `DIGEST_MAX_ENTRIES`."""
    unsendable: tuple[tuple[UUID, str], ...]
    """`(notification id, why)` for rows this pass can never send — the event lost its summary
    or is no longer the published card — marked `failed` with the reason, so a permanently
    un-sendable row cannot stall silently in a backlog read nightly."""
    slate: tuple[SlateDay, ...] = ()

    @property
    def rendered_entries(self) -> tuple[DigestEntry, ...]:
        return self.entries[:DIGEST_MAX_ENTRIES]

    @property
    def overflow(self) -> int:
        """Entries past the cap — the N in "and N more films on your timeline"."""
        return max(0, len(self.entries) - DIGEST_MAX_ENTRIES)

    @property
    def item_ids(self) -> list[UUID]:
        """Every beat's row, rendered or past the cap: all of them are marked `sent` (DC-8)."""
        return [beat.notification_id for entry in self.entries for beat in entry.beats]

    @property
    def slate_count(self) -> int:
        return sum(len(d.items) for d in self.slate)

    @property
    def has_content(self) -> bool:
        return bool(self.entries) or bool(self.slate)


@dataclass
class DigestSendResult:
    """What one digest pass carried."""

    cadence: str
    users_considered: int = 0
    """Users on this cadence with something to look at — a `queued` digest row, or (when the
    slate is in) a follow that might put a date on it. The working set, not the user table."""
    mails_sent: int = 0
    """Mails handed to the provider: inboxes touched, one per user. Reported beside `sent`
    because a slate mail can carry no rows at all."""
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
    """Batches lost to a crash *around* the send — the session, the bookkeeping write — as
    opposed to a provider refusing a mail, which is a `failed` row. Counted separately and
    reported on the detail line because their rows stay `queued` and say nothing themselves:
    without this number a pass that lost forty batches to a database having a bad minute
    reads as `0 failed` on `/admin/runs`."""
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
    session: AsyncSession, *, cadence: DigestCadence, with_slate: bool
) -> list[DigestRecipient]:
    """Every user on `cadence` with something this slot might mail, oldest account first.

    The cadence is `COALESCE`d over an outer join because the settings row is created lazily
    (`app.models.UserSettings`): a user who has never opened the settings screen has no row and
    holds the default, and an inner join would silently drop every one of them from the weekly
    digest — which is the digest most users get.

    The gate is read as a column rather than a filter, because a user it refuses is owed
    `suppressed` rows, not silence (D-39). The `EXISTS` terms keep the pass
    proportional to what is owed rather than to signups: without the slate only a queued row
    puts a user in the set; `with_slate` — the weekly, and the daily on the slate day
    (`carries_slate`) — adds anyone with a **follow**, because the slate is computed from the
    follow graph (M8) and needs no row at all. Without that term a daily reader with an empty
    queue would never be looked at on the slate day, and a full slate would go unsent (DC-2).
    A follow that covers nothing dated costs one empty slate query and no mail — "nothing
    queued and an empty slate gets no mail" already covers it."""
    queued_digest = exists().where(
        Notification.user_id == User.id,
        Notification.kind == DIGEST_KIND,
        Notification.channel == EMAIL_CHANNEL,
        Notification.status == "queued",
    )
    owed = queued_digest
    if with_slate:
        owed = or_(queued_digest, exists().where(Follow.user_id == User.id))
    rows = await session.execute(
        select(
            User.id,
            User.email,
            User.display_name,
            and_(entitled_user_clause(), verified_user_clause()).label("deliverable"),
            UserSettings.unsubscribe_token,
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
            unsubscribe_token=row.unsubscribe_token,
        )
        for row in rows
    ]


async def load_entries(
    session: AsyncSession, *, user_id: UUID, today: date, settings: Settings
) -> tuple[tuple[DigestEntry, ...], tuple[tuple[UUID, str], ...]]:
    """This user's `queued` digest rows as ranked film entries, plus the rows that can never
    be sent.

    The join is the mail's beat half — the film, the event's type, confidence, provenance and
    summary — and the event's status and summary are re-read rather
    than trusted from the queue, because the queue outlives the run that wrote it.
    `EventSummary` is the one outer join so a missing summary comes back as a row to fail
    rather than a row that quietly disappears.

    Then one batched lookup per header input, one for the sources and one for the attribution,
    each over the whole batch rather than per entry: the parenthetical's directors and
    countries and the status line's headline release come from the helpers the feed and the
    film page use, so the mail cannot describe a film differently from the site."""
    rows = await session.execute(
        select(
            Notification.id,
            Event.id.label("event_id"),
            Event.event_type,
            Event.status.label("event_status"),
            Event.confidence,
            Event.provenance,
            Event.created_at,
            Event.occurred_at,
            EventSummary.summary,
            Film.id.label("film_id"),
            Film.tmdb_id,
            Film.title,
            Film.poster_path,
            Film.status.label("film_status"),
            Film.release_date,
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
    by_film: dict[UUID, list[Row[Any]]] = {}
    for row in rows:
        if row.event_status != "published":
            unsendable.append((row.id, "the event is no longer published"))
        elif row.summary is None:
            unsendable.append((row.id, "the event has no summary"))
        else:
            by_film.setdefault(row.film_id, []).append(row)
    if not by_film:
        return (), tuple(unsendable)

    film_ids = set(by_film)
    film_of = {row.event_id: film_id for film_id, rs in by_film.items() for row in rs}
    titles = {film_id: rs[0].title for film_id, rs in by_film.items()}
    directors = await _directors_for_films(session, film_ids)
    countries = await _production_countries_for_films(session, film_ids)
    releases = await headline_releases(session, film_ids, today=today)
    story_events = [
        row.event_id for rs in by_film.values() for row in rs if row.provenance == "story"
    ]
    sources = await _first_sources(session, story_events)
    following = await _load_following(
        session, user_id=user_id, film_of=film_of, titles=titles, settings=settings
    )

    entries: list[DigestEntry] = []
    for film_id, film_rows in by_film.items():
        film = film_rows[0]
        arc_stage = derive_arc_stage(film.film_status)
        beats = [
            DigestBeat(
                notification_id=row.id,
                event_id=row.event_id,
                event_type=row.event_type,
                created_at=row.created_at,
                occurred_at=row.occurred_at,
                confidence=row.confidence,
                summary=row.summary,
                source=sources.get(row.event_id) if row.provenance == "story" else CATALOG_SOURCE,
            )
            for row in film_rows
        ]
        entries.append(
            DigestEntry(
                film=DigestFilm(
                    film_id=film_id,
                    tmdb_id=film.tmdb_id,
                    title=film.title,
                    film_url=film_url(film.tmdb_id, film.title, settings.public_base_url),
                    poster_url=poster_url(film.poster_path, settings.tmdb_image_base),
                    lead_poster_url=poster_url(
                        film.poster_path, settings.tmdb_image_base, size=LEAD_POSTER_SIZE
                    ),
                ),
                header=DigestHeader(
                    parenthetical=film_parenthetical(
                        production_countries=countries.get(film_id, []),
                        directors=directors.get(film_id, []),
                        release_year=_release_year(film.release_date),
                        arc_stage=arc_stage,
                    ),
                    status=status_line(releases.get(film_id), arc_stage),
                ),
                following=following.get(film_id, ()),
                beats=tuple(sorted(beats, key=beat_order_key)),
            )
        )
    return rank_entries(entries), tuple(unsendable)


async def _first_sources(session: AsyncSession, event_ids: list[UUID]) -> dict[UUID, DigestSource]:
    """Each story card's first source in the order `EventOut.sources` lists them — newest
    distinct outlet first (`cap_sources`) — named by outlet and linked (DC-5). A card with no
    story row left is absent, and gets no source line."""
    first: dict[UUID, DigestSource] = {}
    for event_id, stories in (await _sources_by_event(session, event_ids)).items():
        capped = cap_sources(stories)
        if capped:
            first[event_id] = DigestSource(name=outlet_label(capped[0]), url=source_url(capped[0]))
    return first


_ENTITY_TABLES: dict[str, type[Person] | type[ProductionCompany] | type[Collection]] = {
    "person": Person,
    "company": ProductionCompany,
    "franchise": Collection,
}
_ENTITY_REFS = {"person": person_ref, "company": company_ref, "franchise": collection_ref}


async def _load_following(
    session: AsyncSession,
    *,
    user_id: UUID,
    film_of: dict[UUID, UUID],
    titles: dict[UUID, str],
    settings: Settings,
) -> dict[UUID, tuple[DigestFollowing, ...]]:
    """Per film, the follows that reached any of its beats in this batch (DC-6), named and
    linked: `follow_attribution_pairs` narrowed to the batch's event ids, then one name lookup
    per entity table. Person, studio, franchise, then by name; the title row last.

    An entity id the catalog no longer holds is dropped from the line rather than rendered as
    a bare number — a name nobody can read is worse than one fewer name."""
    pairs = follow_attribution_pairs(user_id).subquery("pairs")
    rows = await session.execute(
        select(pairs.c.entity_type, pairs.c.entity_id, pairs.c.event_id).where(
            pairs.c.event_id.in_(list(film_of))
        )
    )
    reached: dict[UUID, set[tuple[str, str]]] = {}
    for entity_type, entity_id, event_id in rows:
        reached.setdefault(film_of[event_id], set()).add((entity_type, entity_id))

    wanted: dict[str, set[int]] = {}
    for pairs_of_film in reached.values():
        for entity_type, entity_id in pairs_of_film:
            if entity_type in _ENTITY_TABLES and entity_id.isdigit():
                wanted.setdefault(entity_type, set()).add(int(entity_id))
    names: dict[tuple[str, int], str] = {}
    for entity_type, ids in wanted.items():
        table = _ENTITY_TABLES[entity_type]
        for entity_id, name in await session.execute(
            select(table.id, table.name).where(table.id.in_(ids))
        ):
            names[(entity_type, entity_id)] = name

    base = settings.public_base_url.rstrip("/")
    following: dict[UUID, tuple[DigestFollowing, ...]] = {}
    for film_id, pairs_of_film in reached.items():
        rows_of_film: list[DigestFollowing] = []
        for entity_type, entity_id in pairs_of_film:
            if entity_type == "title":
                rows_of_film.append(DigestFollowing("title", titles[film_id], None))
                continue
            name = names.get((entity_type, int(entity_id))) if entity_id.isdigit() else None
            if name is None:
                continue
            ref = _ENTITY_REFS[entity_type](int(entity_id), name)
            url = f"{base}/{_FOLLOWING_ROUTES[entity_type]}/{ref}"
            rows_of_film.append(DigestFollowing(entity_type, name, url))
        following[film_id] = tuple(
            sorted(
                rows_of_film,
                key=lambda f: (_FOLLOWING_ORDER.index(f.entity_type), f.name.casefold()),
            )
        )
    return following


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

    Each row's marker is `load_slate_markers`' answer for its (film, bucket).
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
                Film.id.label("film_id"),
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
    markers = await load_slate_markers(session, film_ids={row.film_id for row in rows}, today=today)
    by_day: dict[date, list[SlateItem]] = {}
    for row in ordered:
        by_day.setdefault(row.governing_date, []).append(
            SlateItem(
                title=row.title,
                release_label=release_label(row.release_type),
                film_url=film_url(row.tmdb_id, row.title, settings.public_base_url),
                poster_url=poster_url(row.poster_path, settings.tmdb_image_base),
                marker=markers.get((row.film_id, RELEASE_TYPE_BUCKETS[row.release_type])),
            )
        )
    return tuple(SlateDay(day=day, items=tuple(items)) for day, items in by_day.items())


def slate_marker(changes: Iterable[str]) -> SlateMarker | None:
    """One slate row's marker from the changes its date went through inside the marker window
    (DC-9), each `set` or `moved` as `film_release_date_change` records them. Pure.

    None when there were none. **new** when any of them *set* the date — the subject had no
    date on the previous slate day, so relative to that slate the date is new however often it
    has moved since; **moved** otherwise."""
    seen = set(changes)
    if not seen:
        return None
    return "new" if RELEASE_DATE_SET in seen else "moved"


def change_from_summary(summary: str | None, bucket: str) -> str:
    """The fallback for a card whose change is not persisted (DC-9): the deterministic body's
    own verb for this market — "US wide release date set to …" is a `set`, and anything else
    ("moved from", "slipped from", or a body an admin rewrote) is read as `moved`, the spec's
    "moved otherwise"."""
    if summary is not None and f"{PRIMARY_REGION} {bucket} release date set to" in summary:
        return RELEASE_DATE_SET
    return RELEASE_DATE_MOVED


async def load_slate_markers(
    session: AsyncSession, *, film_ids: set[UUID], today: date
) -> dict[tuple[UUID, str], SlateMarker]:
    """Per (film, bucket), the slate marker the row carries (DC-9) — absent for a date nothing
    set or moved in the `SLATE_MARKER_DAYS` window ending on `today`.

    The cards read are the published `release_date` cards on these films whose `subject_key`
    covers a `US:<bucket>` token (D-26) and whose `created_at` falls in the window. Only the
    sweep's catalog cards carry those tokens — a story-borne release-date card has no subject,
    and the sweep refuses to card a second time a move a story already reported — so this is
    a read of the same observations the slate's dates came from.

    Whether a card set or moved a market's date is read from the **persisted change**: the
    card's `occurred_at` is the observation's `changed_at` (`sweep.release_events`), so the
    `film_release_date_change` row for (film, `changed_at`, US, type) is the change itself.
    Only a card with no such row falls back to the verb in its summary
    (`change_from_summary`)."""
    if not film_ids:
        return {}
    window_start = datetime.combine(
        today - timedelta(days=SLATE_MARKER_DAYS - 1), time.min, tzinfo=UTC
    )
    window_end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)
    tokens = [f"{PRIMARY_REGION}:{bucket}" for bucket in _SLATE_BUCKET_ORDER]
    rows = await session.execute(
        select(
            Event.id,
            Event.film_id,
            Event.subject_key,
            EventSummary.summary,
            FilmReleaseDateChange.release_type,
            FilmReleaseDateChange.change,
        )
        .outerjoin(EventSummary, EventSummary.event_id == Event.id)
        .outerjoin(
            FilmReleaseDateChange,
            and_(
                FilmReleaseDateChange.film_id == Event.film_id,
                FilmReleaseDateChange.changed_at == Event.occurred_at,
                FilmReleaseDateChange.iso_3166_1 == PRIMARY_REGION,
            ),
        )
        .where(
            Event.film_id.in_(film_ids),
            Event.event_type == "release_date",
            Event.status == "published",
            Event.created_at >= window_start,
            Event.created_at < window_end,
            Event.subject_key.overlap(tokens),
        )
    )
    cards: dict[UUID, tuple[UUID, list[str], str | None]] = {}
    persisted: dict[UUID, dict[str, str]] = {}
    for row in rows:
        cards[row.id] = (row.film_id, row.subject_key, row.summary)
        bucket = (
            RELEASE_TYPE_BUCKETS.get(row.release_type) if row.release_type is not None else None
        )
        if bucket is not None:
            persisted.setdefault(row.id, {})[bucket] = row.change
    changes: dict[tuple[UUID, str], list[str]] = {}
    prefix = f"{PRIMARY_REGION}:"
    for card_id, (film_id, subject_key, summary) in cards.items():
        for token in subject_key:
            if not token.startswith(prefix):
                continue
            bucket = token.removeprefix(prefix)
            change = persisted.get(card_id, {}).get(bucket) or change_from_summary(summary, bucket)
            changes.setdefault((film_id, bucket), []).append(change)
    return {
        key: marker for key, seen in changes.items() if (marker := slate_marker(seen)) is not None
    }


async def load_batch(
    session: AsyncSession,
    *,
    recipient: DigestRecipient,
    cadence: DigestCadence,
    today: date,
    settings: Settings,
    include_slate: bool | None = None,
) -> DigestBatch:
    """Everything one user's digest would carry on this cadence.

    The slate is loaded only when the cadence carries it today (`carries_slate`: the weekly,
    or the daily on the slate day) *and* only for a user the gate admits: for a refused user
    the answer is already "no mail", and reading their follows would be work in service of a
    section that must not be sent (D-39). `include_slate` overrides that rule when given —
    `render_digest` uses it to show a lapsed user's slate to an admin without touching the
    send path."""
    entries, unsendable = await load_entries(
        session, user_id=recipient.user_id, today=today, settings=settings
    )
    if include_slate is None:
        include_slate = carries_slate(cadence, today, settings) and recipient.deliverable
    slate: tuple[SlateDay, ...] = ()
    if include_slate:
        slate = await load_slate(session, user_id=recipient.user_id, today=today, settings=settings)
    return DigestBatch(recipient=recipient, entries=entries, unsendable=unsendable, slate=slate)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _headline(entry: DigestEntry) -> str:
    """ "Heat 2 — casting": an entry as the subject and the preheader name it."""
    return f"{entry.film.title} — {entry.lead_label.lower()}"


def digest_subject(batch: DigestBatch) -> str:
    """The subject line (DC-7): the lead film and its lead beat, how many more films, and
    whether the slate is in — or, for a slate alone, how many dates. `N` counts every entry
    past the lead, those past the cap included, so the subject and the cap line agree. Never
    a count of beats.

    Raises on an empty batch: `render_batch` never builds one, and a caller that skipped that
    check should fail loudly rather than send a subject about nothing."""
    if not batch.entries:
        if not batch.slate:
            raise ValueError("a digest with no entries and no slate has no subject")
        return f"Your slate: {_plural(batch.slate_count, 'upcoming date')}"
    subject = _headline(batch.entries[0])
    more = len(batch.entries) - 1
    if more:
        subject += f", + {_plural(more, 'more film')}"
    if batch.slate:
        subject += " · your slate"
    return subject


def digest_preheader(batch: DigestBatch) -> str:
    """The inbox preview text (DC-10): what the subject had no room for — the slate's size,
    then up to two entries after the lead. Empty when there is nothing to add, and the
    template then omits the element rather than rendering it empty."""
    parts: list[str] = []
    if batch.slate:
        parts.append(
            f"Your slate: {_plural(batch.slate_count, 'date')} in the next "
            f"{SLATE_WINDOW_DAYS} days."
        )
    also = batch.entries[1:3]
    if also:
        parts.append("Also: " + "; ".join(_headline(entry) for entry in also))
    return " ".join(parts)


def _entry_context(entry: DigestEntry, *, poster_url: str | None, today: date) -> dict[str, object]:
    """One film entry as the template renders it, lead card and compact row alike — they
    differ in layout and poster size only, and the size is the caller's choice."""
    return {
        "title": entry.film.title,
        "film_url": entry.film.film_url,
        "poster_url": poster_url,
        "parenthetical": entry.header.parenthetical,
        "status": entry.header.status,
        "following": [{"name": f.name, "url": f.url} for f in entry.entity_following],
        "beats": [
            {
                "date": short_date(beat.day, today=today),
                "label": beat.label,
                "unconfirmed": beat.confidence == "rumored",
                "summary": beat.summary,
                "source": (
                    {"name": beat.source.name, "url": beat.source.url}
                    if beat.source is not None
                    else None
                ),
            }
            for beat in entry.beats
        ],
        "credits_justwatch": entry.credits_justwatch,
    }


def digest_context(
    batch: DigestBatch, *, cadence: DigestCadence, today: date, settings: Settings
) -> dict[str, object]:
    """The `digest` template's context for one batch — the only place it is built. Every
    string the mail shows is decided here; the templates lay it out and compute nothing.

    Raises `ValueError` (through `digest_subject`) for a batch with nothing to say:
    `render_batch` never builds one, so a caller that does has skipped that check."""
    # The lead film renders as the lead card, every other entry as a compact row (DC-14).
    # Split here rather than on `loop.first` in the template, so which entry leads is decided
    # in the one place the subject's lead film is (DC-7).
    rendered = batch.rendered_entries
    lead = rendered[0] if rendered else None
    return {
        "product_name": settings.product_name,
        "display_name": batch.recipient.display_name,
        "settings_url": settings_url(settings.public_base_url),
        "cadence": cadence,
        "slate_window_days": SLATE_WINDOW_DAYS,
        "subject": digest_subject(batch),
        "preheader": digest_preheader(batch),
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
                        "marker": item.marker,
                    }
                    for item in d.items
                ],
            }
            for d in batch.slate
        ],
        "lead": (
            _entry_context(lead, poster_url=lead.film.lead_poster_url, today=today)
            if lead is not None
            else None
        ),
        "entries": [
            _entry_context(entry, poster_url=entry.film.poster_url, today=today)
            for entry in rendered[1:]
        ],
        "overflow": batch.overflow,
        "overflow_line": (
            f"and {_plural(batch.overflow, 'more film')} on your timeline" if batch.overflow else ""
        ),
        "timeline_url": f"{settings.public_base_url.rstrip('/')}/",
        "unsubscribe_url": recipient_unsubscribe_url(batch.recipient, settings),
    }


def recipient_unsubscribe_url(recipient: DigestRecipient, settings: Settings) -> str | None:
    """The recipient's one-click unsubscribe link, or None when they have no token yet — on the
    API's origin, not the site's, because a mailbox provider POSTs to it directly (DC-10)."""
    if recipient.unsubscribe_token is None:
        return None
    base = settings.api_base_url.rstrip("/")
    return f"{base}/digest/unsubscribe/{recipient.unsubscribe_token}"


def unsubscribe_headers(link: str) -> dict[str, str]:
    """The RFC 2369 / RFC 8058 pair that puts an Unsubscribe button in the mailbox's own UI and
    lets it act with one POST (DC-10)."""
    return {
        "List-Unsubscribe": f"<{link}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


async def ensure_unsubscribe_token(session: AsyncSession, *, user_id: UUID) -> str:
    """This user's unsubscribe token, creating their settings row if they have none. The
    caller commits.

    The one settings row a batch pass writes (see `settings_service`): only `send_batch` calls
    this, and only for a user it is about to mail. The row is the defaults
    `settings_service.get_or_create` would write, so the digest cadence it records is the
    `weekly` default this user was already on — creating it changes nothing they receive."""
    row = await user_settings_repo.create_if_absent(
        session,
        user_id=user_id,
        ical_token=tokens.new_ical_token(),
        unsubscribe_token=tokens.new_unsubscribe_token(),
    )
    return row.unsubscribe_token


def render_batch(
    batch: DigestBatch, *, cadence: DigestCadence, today: date, settings: Settings
) -> Envelope | None:
    """The batch as the mail it would be, or None when it has nothing to say (DC-13). The one
    render path: `send_batch` delivers this, and `render_digest` returns it (D-1460.1)."""
    if not batch.has_content:
        return None
    envelope = render(
        DIGEST_TEMPLATE,
        digest_context(batch, cadence=cadence, today=today, settings=settings),
        sender=settings.mail_from,
        to=batch.recipient.email,
    )
    link = recipient_unsubscribe_url(batch.recipient, settings)
    if link is None:
        return envelope
    return replace(envelope, headers=unsubscribe_headers(link))


async def render_digest(
    session: AsyncSession,
    user_id: UUID,
    cadence: DigestCadence,
    today: date,
    settings: Settings,
    *,
    with_unsubscribe: bool = True,
) -> Envelope | None:
    """The digest this user would get on `cadence` today, rendered and not sent — what the
    admin preview and test-send call, and nothing else (M3).

    The gate is ignored (`deliverable=True`) and the slate follows the cadence and the day
    alone (`carries_slate`), so a daily preview on the slate day shows the slate: an admin
    looking at a lapsed user's mail wants to see what it would say, and whether it would be
    *sent* is `send_digests`' question, which still answers it. Marks nothing, commits
    nothing. None when there is nothing to say; `LookupError` for an unknown user.

    `with_unsubscribe=False` renders the mail as a rowless user's would be — no
    `List-Unsubscribe` header and no token link in the footer. The test-send needs that: the
    token turns *this user's* digest off, and a copy of it in the admin's inbox is one
    link-scanner prefetch away from doing so (the unsubscribe GET writes; see
    `routers/digest.py`'s module docstring)."""
    user = (
        await session.execute(
            select(User.email, User.display_name, UserSettings.unsubscribe_token)
            .outerjoin(UserSettings, UserSettings.user_id == User.id)
            .where(User.id == user_id)
        )
    ).one_or_none()
    if user is None:
        raise LookupError(f"no user {user_id}")
    recipient = DigestRecipient(
        user_id=user_id,
        email=user.email,
        display_name=user.display_name,
        deliverable=True,
        unsubscribe_token=user.unsubscribe_token if with_unsubscribe else None,
    )
    batch = await load_batch(
        session,
        recipient=recipient,
        cadence=cadence,
        today=today,
        settings=settings,
        include_slate=carries_slate(cadence, today, settings),
    )
    return render_batch(batch, cadence=cadence, today=today, settings=settings)


async def send_test_digest(
    session: AsyncSession,
    *,
    user_id: UUID,
    cadence: DigestCadence,
    today: date,
    to: str,
    mailer: Mailer,
    settings: Settings,
) -> MessageId | None:
    """Mail this user's digest to `to` instead of to them — the admin test-send (DC-11).

    The same `render_digest` the preview shows, readdressed, with the subject prefixed
    `[test for <user email>] ` so the copy cannot pass for the admin's own digest in their
    inbox. Rendered without the unsubscribe token (see `render_digest`), so neither the header
    nor the footer can turn the user's digest off from the admin's mailbox. Marks nothing,
    commits nothing. None when there is nothing to send; `LookupError` for an unknown user;
    the provider's errors propagate."""
    envelope = await render_digest(
        session, user_id, cadence, today, settings, with_unsubscribe=False
    )
    if envelope is None:
        return None
    return await mailer.deliver(
        replace(envelope, to=to, subject=f"[test for {envelope.to}] {envelope.subject}")
    )


async def send_batch(
    session: AsyncSession,
    *,
    batch: DigestBatch,
    cadence: DigestCadence,
    today: date,
    mailer: Mailer,
    settings: Settings,
) -> DigestOutcome:
    """Send one user's digest and record every row's outcome. The caller commits.

    The gate is answered first and is the whole answer for a refused user: every row they hold
    is `suppressed`, including the ones with no copy, and the slate — already withheld by
    `load_batch` — is not sent. Then the un-sendable rows are failed by reason, and only then is
    there a mail to send, and only if there is something to put in it. The render sits inside
    the provider's `except` so a template fault fails the rows rather than stalling them, as it
    did when `Mailer.send` rendered. The errors caught are the set `verification_service.send`
    catches, for the same reason: the two `RuntimeError`s are raised when the gateway *builds*
    its transport, on the first send of the process, and an unlikely configuration fault should
    mark a batch `failed` with a reason rather than crash the run that would have reported it.
    Every row is marked `sent`, the ones past the cap included (DC-8)."""
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
    if batch.has_content and batch.recipient.unsubscribe_token is None:
        token = await ensure_unsubscribe_token(session, user_id=batch.recipient.user_id)
        batch = replace(batch, recipient=replace(batch.recipient, unsubscribe_token=token))
    try:
        envelope = render_batch(batch, cadence=cadence, today=today, settings=settings)
        if envelope is None:
            return DigestOutcome(failed=failed)
        await mailer.deliver(envelope)
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
    `pipeline_run.run_digest_stage`. The abort guard counts provider refusals because a provider
    is one shared dependency — ten consecutive refusals are one outage (a rotated key, a
    suspended account), not ten unrelated faults — and a run of them must fail the run, putting
    the deadman red, rather than convert the backlog into `failed` rows under a green check.
    """
    if cadence not in SEND_CADENCES:
        raise ValueError(f"digest cadence must be one of {SEND_CADENCES}, not {cadence!r}")
    result = DigestSendResult(cadence=cadence)
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        recipients = await load_recipients(
            s, cadence=cadence, with_slate=carries_slate(cadence, today, settings)
        )
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
                    s, batch=batch, cadence=cadence, today=today, mailer=mailer, settings=settings
                )
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except Exception:
            # A crash around the send leaves the rows `queued` for the next slot — with one
            # honest caveat. If the crash landed between the provider accepting the message and
            # this commit, the reader gets it again next time. Marking `sent` before sending
            # turns that window into a mail nobody gets; a duplicate is the better failure.
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
