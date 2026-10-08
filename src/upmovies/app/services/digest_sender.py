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

**One mail per user per run, and the daily is the timeline day, reproduced (FB-18,
ADR-0022).** Each queued row is a card; `follow_attribution_pairs` says which of the user's
follows reached it — the title arm a title follow, the entity arms a person, studio or
franchise follow — and the card becomes one **line** per reach, exactly as the timeline makes
it one row per reach (FB-5, FB-13). The lines are then laid out as the timeline lays out a day
(`group_days`): publication day (UTC `created_at`, ADR-0016) newest first, its poster strip,
then the follow blocks (Films, People, Studios, Franchises), each split into In the news and
Not yet reported, the latter by update type — the feed's map (NR-3) under Films, Attached /
Detached / Canceled / Other updates under the others (FB-4). Under Films a row is a **film
row** headed by the film (title and parenthetical, linked); under the other three it is an
**entity row** headed by the followed entity, each line naming its film (FB-20). Nothing is
cut and nothing leads: every row renders, and every row is marked `sent` (FB-21).

**The weekly reads by entry, not by day (FB-19, `group_week`).** The same blocks, sections and
update types, but no day headings: one **film entry** per film and one **entity entry** per
entity across the week, under each section and update type it touched, its lines in
publication order and each dated; one poster strip over the week's films. A film with cards in
both sections is an entry in both.

A card no follow reaches any more — the reader unfollowed between the decision pass and this
one — is not a line: the timeline no longer shows it, so the mail does not either, and its row
is failed with the reason rather than left `queued` (see below).

The subject and preheader (DC-7, DC-10) are computed over every line of every reach: the
**lead film** is the film carrying the most significant beat in the mail (a film reached
only through a studio can lead), and it is not rendered differently anywhere (FB-22).

**One render path (D-1460.1).** `render_batch` turns a batch into an `Envelope`; `send_batch`
hands that to `Mailer.deliver`, and `render_digest` — what the admin preview and test-send
call — returns it without sending. `digest_context` is the only place the template's dict is
built, so the three cannot disagree about what the mail says.

**The weekly send is the "your slate" mail (D-33), and so is the daily one on the slate day
(DC-2).** The weekly always carries the slate; the daily carries it only when the run's `today`
falls on `SLATE_WEEKDAY`, so a daily reader sees upcoming dates once a week, on the day the
weekly readers do. Before the timeline section it is **the my-films calendar reproduced** for
the next `SLATE_WINDOW_DAYS` (FB-26): the rows `public.service._calendar_page` builds for this
user's title follows (EF-14), read from `film_release_date` directly rather than from
notification rows — a date that has not *moved* produces no event, and the slate's job is to say
what is coming, not what changed — and laid out as the calendar page lays them out: date →
release-type bucket → film row, under month headings only across a month boundary. The rows
are the calendar's own, so the slate cannot name a date, or describe a film, differently from
it. A date **set or moved since the previous slate
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
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID

import httpx
from sqlalchemy import Row, and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app import tokens
from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.entity_names import entity_names
from upmovies.app.follow_queries import follow_attribution_pairs
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
from upmovies.catalog.models import (
    Film,
    FilmReleaseDateChange,
)
from upmovies.catalog.ref import film_ref, parse_film_ref
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
from upmovies.public.dto import CalendarItem
from upmovies.public.release import RELEASE_BUCKET_LABELS
from upmovies.public.service import (
    _CALENDAR_BUCKET_ORDER,
    _calendar_governing_cte,
    _calendar_page,
    _directors_for_films,
    _has_story,
    _my_films_visible,
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

MAX_DAY_POSTERS = 8
"""How many posters a day's strip carries at most — the feed's `MAX_DAY_POSTERS`
(`lib/feed-groups.ts`), so a backfill-tall day does not fetch dozens of images (FB-18)."""

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

SLATE_POSTER_SIZE = "w92"
"""The my-films calendar row's poster (`CalendarFilmRow`): the slate is that row (FB-26), so it
fetches the image the calendar page fetches, not the timeline's `POSTER_SIZE`."""

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

DIGEST_BEAT_LABELS: dict[str, str] = {
    "release_date": "Release date",
    "now_available": "Now available",
    "trailer": "New trailer",
    "announced": "Announced",
    "casting": "Casting",
    "crew_attached": "Crew attached",
    "cast_removed": "Cast departure",
    "crew_removed": "Crew departure",
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

OTHER_UPDATES = "other"
"""The update type any `event_type` a map does not know files under, in both maps — and the
one named heading under which the beat label stays on the line (NR-5)."""

FILM_UPDATE_TYPES: tuple[tuple[str, str], ...] = (
    ("now_available", "Now available"),
    ("trailer", "Trailer"),
    ("release_date", "Release date"),
    ("production_status", "Production status"),
    ("cast", "Cast"),
    ("crew", "Crew"),
    ("studios", "Studios"),
    ("franchise", "Franchise"),
    (OTHER_UPDATES, "Other updates"),
)
"""The Films block's Not yet reported headings, in render order, with their labels — the
frontend's `UPDATE_TYPES` and `UPDATE_TYPE_LABELS` (NR-3), word for word, because the daily
is the timeline day reproduced."""

_FILM_UPDATE_TYPE_OF_EVENT: dict[str, str] = {
    "now_available": "now_available",
    "trailer": "trailer",
    "release_date": "release_date",
    "production_start": "production_status",
    "production_wrap": "production_status",
    "canceled": "production_status",
    "casting": "cast",
    "cast_removed": "cast",
    "crew_attached": "crew",
    "crew_removed": "crew",
    "company_attached": "studios",
    "company_removed": "studios",
    "collection_attached": "franchise",
    "collection_removed": "franchise",
}

ENTITY_UPDATE_TYPES: tuple[tuple[str, str], ...] = (
    ("attached", "Attached"),
    ("detached", "Detached"),
    ("canceled", "Canceled"),
    (OTHER_UPDATES, "Other updates"),
)
"""The People, Studios and Franchises blocks' Not yet reported headings (FB-4) — the
frontend's `ENTITY_UPDATE_TYPES`. A second map beside `FILM_UPDATE_TYPES`, never merged with
it: `casting` is Cast under Films and Attached under People."""

_ENTITY_UPDATE_TYPE_OF_EVENT: dict[str, str] = {
    "casting": "attached",
    "crew_attached": "attached",
    "company_attached": "attached",
    "collection_attached": "attached",
    "cast_removed": "detached",
    "crew_removed": "detached",
    "company_removed": "detached",
    "collection_removed": "detached",
    "canceled": "canceled",
}

FILMS_BLOCK = "films"
FOLLOW_BLOCKS: tuple[tuple[str, str], ...] = (
    (FILMS_BLOCK, "Films"),
    ("people", "People"),
    ("studios", "Studios"),
    ("franchises", "Franchises"),
)
"""The follow blocks a day is laid out in, in their fixed order, with their headings (FB-1) —
the frontend's `FOLLOW_BLOCKS` and `FOLLOW_BLOCK_LABELS`."""

_BLOCK_OF_ENTITY_TYPE: dict[str, str] = {
    "person": "people",
    "company": "studios",
    "franchise": "franchises",
}

ENTITY_FALLBACK_NAMES: dict[str, str] = {
    "person": "A person you follow",
    "company": "A studio you follow",
    "franchise": "A franchise you follow",
}
"""An entity row's headline when the catalog can no longer name the entity (FB-10) — the
frontend's `ENTITY_FALLBACK_NAMES`. Rendered unlinked: there is no ref to link."""

IN_THE_NEWS_LABEL = "In the news"
NOT_YET_REPORTED_LABEL = "Not yet reported"
NOT_YET_REPORTED_QUALIFIER = "(unconfirmed)"
"""The section headings, as the feed spells them (`components/film/labels.ts`): the qualifier
is said once for the section, which is why its lines carry no Unconfirmed marker (NR-5)."""

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
    return ref_url(film_ref(tmdb_id, title), base_url)


def ref_url(ref: str, base_url: str) -> str:
    """The film page for a ref already minted — a calendar row carries its own (FB-26)."""
    return f"{base_url.rstrip('/')}/film/{ref}"


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
countries and two directors, and the mail's film row is that row."""

_ENTITY_ROUTES: dict[str, str] = {
    "person": "person",
    "company": "studio",
    "franchise": "franchise",
}
"""Follow `entity_type` → the frontend route segment its page lives under. The follow graph's
words (`company`, `franchise`) are not the reader's (EF-19)."""

_LEADING_ARTICLE = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)


def natural_title(title: str) -> str:
    """The feed's natural sort key for a title or an organisation's name: leading "A", "An",
    "The" stripped, casefolded (`public.service._natural_title_col`, the frontend's
    `naturalSortKey`)."""
    return _LEADING_ARTICLE.sub("", title).casefold()


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


def short_date(d: date, *, today: date) -> str:
    """ "22 Sep" — a beat's publication day (DC-5, DC-12). The year is appended only when it is
    not the run's year, which a tall digest after months of `off` can span. `today` is the
    run's, never the wall clock's, so a test's answer does not depend on when it runs."""
    day = f"{d.day} {_MONTHS[d.month - 1][:3]}"
    return day if d.year == today.year else f"{day} {d.year}"


@dataclass(frozen=True)
class DigestSource:
    """Where a beat came from, as one line under it (DC-5): the first story's outlet, linked.
    A card no story covers has none — under Not yet reported the section heading says where it
    came from, and "via TMDB" is not repeated on every line (FB-20)."""

    name: str
    url: str | None


@dataclass(frozen=True)
class DigestFilm:
    """The film a beat is about, resolved to what its row or line links and shows."""

    film_id: UUID
    tmdb_id: int
    title: str
    film_url: str
    poster_url: str | None
    """The poster strip's (`w154`); None for a film with no poster, which the strip skips."""
    parenthetical: str

    @property
    def sort_key(self) -> tuple[str, str, int]:
        """Natural title, then the raw title casefolded, then `tmdb_id` — so two films equal
        under the natural key still sort the same way twice."""
        return natural_title(self.title), self.title.casefold(), self.tmdb_id


@dataclass(frozen=True)
class DigestReach:
    """A person, studio or franchise follow that reached a card — an entity row's headline.
    A card a title follow reached has no `DigestReach`: its reach is the film itself."""

    entity_type: str
    """`person`, `company` or `franchise` — the follow graph's words."""
    entity_id: str
    name: str | None
    """The catalog's current name; None for an entity it cannot name (FB-10)."""
    url: str | None
    """The entity's page; None exactly when `name` is."""

    @property
    def headline(self) -> str:
        return self.name if self.name is not None else ENTITY_FALLBACK_NAMES[self.entity_type]

    @property
    def sort_key(self) -> tuple[bool, str, str]:
        """FB-6: by name — a person's as written, casefolded ("The" is not how a person's name
        starts), a studio's or franchise's as a title sorts. An entity with no name trails, and
        `entity_id` breaks ties."""
        name = self.name or ""
        key = name.casefold() if self.entity_type == "person" else natural_title(name)
        return self.name is None, key, self.entity_id


@dataclass(frozen=True)
class DigestBeat:
    """One queued card, as the mail shows it on any line it lands on."""

    notification_id: UUID
    event_id: UUID
    event_type: str
    created_at: datetime
    occurred_at: datetime
    confidence: str
    """`confirmed` or `rumored`; a rumored beat carries the Unconfirmed marker where the
    section shows markers at all."""
    summary: str
    source: DigestSource | None
    news_backed: bool
    """Whether a story covers the card — the feed's section split (`EXISTS(event_story)`),
    deliberately not `provenance`."""
    film: DigestFilm

    @property
    def day(self) -> date:
        """The publication day, in UTC, exactly as the feed keys it (ADR-0016)."""
        return self.created_at.astimezone(UTC).date()

    @property
    def label(self) -> str:
        return digest_beat_label(self.event_type)


@dataclass(frozen=True)
class DigestLine:
    """A card as one reach delivered it: the timeline's row grain (FB-13), one level finer. A
    card two follows reached is two lines."""

    beat: DigestBeat
    reach: DigestReach | None
    """None for a title follow — the line belongs to a film row in the Films block."""

    @property
    def block(self) -> str:
        if self.reach is None:
            return FILMS_BLOCK
        return _BLOCK_OF_ENTITY_TYPE[self.reach.entity_type]


def event_order_key(beat: DigestBeat) -> tuple[datetime, datetime, str]:
    """The feed's event order within a row — `occurred_at`, `created_at`, id (FB-6) — which is
    the daily's: its day heading already says when each line was published."""
    return beat.occurred_at, beat.created_at, str(beat.event_id)


def film_then_event_order_key(beat: DigestBeat) -> tuple[Any, ...]:
    """A daily entity row's line order: its film's natural title, then the feed's event order
    (FB-6) — the timeline's entity row."""
    return *beat.film.sort_key, *event_order_key(beat)


def beat_order_key(beat: DigestBeat) -> tuple[datetime, datetime, str]:
    """**Publication** order — `created_at`, then `occurred_at`, then the event id so two beats
    published together still sort the same way twice (DC-3). The weekly's: an entry's lines
    span days, so they run in the order the reader could have seen them (FB-19)."""
    return beat.created_at, beat.occurred_at, str(beat.event_id)


BeatKey = Callable[[DigestBeat], tuple[Any, ...]]


@dataclass(frozen=True)
class LineOrder:
    """How the lines under a row run — a film row's and an entity row's. The one thing a
    daily row and a weekly entry lay out differently below the update type."""

    film_row: BeatKey
    entity_row: BeatKey


DAY_LINE_ORDER = LineOrder(film_row=event_order_key, entity_row=film_then_event_order_key)
"""The daily's (FB-6): the timeline day's rows, whose day heading already dates every line."""

ENTRY_LINE_ORDER = LineOrder(film_row=beat_order_key, entity_row=beat_order_key)
"""The weekly's (FB-19): an entry's lines in publication order, film and entity entries alike
— an entity entry spanning two films reads as the week went, not film by film."""


@dataclass(frozen=True)
class DigestRow:
    """A **film row** (`reach` None: one film, headed by it) or an **entity row** (one
    followed entity, headed by it, each beat naming its own film) under one section or update
    type."""

    reach: DigestReach | None
    beats: tuple[DigestBeat, ...]

    @property
    def film(self) -> DigestFilm:
        """A film row's film. Meaningless on an entity row, whose beats name several."""
        return self.beats[0].film

    @property
    def lead_type(self) -> str:
        return most_significant_event_type(beat.event_type for beat in self.beats)

    @property
    def credits_justwatch(self) -> bool:
        return any(beat.event_type == JUSTWATCH_EVENT_TYPE for beat in self.beats)


def film_row_key(row: DigestRow) -> tuple[int, str, str, int]:
    """Film rows under a section or update type: the more significant beat first, then the
    film's natural title — the timeline day's own order for its title rows."""
    return (-event_stage_rank(row.lead_type), *row.film.sort_key)


def entity_row_key(row: DigestRow) -> tuple[bool, str, str]:
    assert row.reach is not None
    return row.reach.sort_key


@dataclass(frozen=True)
class DigestUpdateType:
    """One update-type heading under a Not yet reported section, holding only its own beats."""

    key: str
    label: str
    rows: tuple[DigestRow, ...]


@dataclass(frozen=True)
class DigestSection:
    """In the news (`news_backed`), laid out by row, or Not yet reported, laid out by update
    type. Exactly one of `rows` and `update_types` is filled."""

    news_backed: bool
    rows: tuple[DigestRow, ...] = ()
    update_types: tuple[DigestUpdateType, ...] = ()


@dataclass(frozen=True)
class DigestBlock:
    key: str
    label: str
    sections: tuple[DigestSection, ...]


@dataclass(frozen=True)
class DigestDay:
    """One timeline day of the daily (FB-18): its heading's day, its poster strip, its
    blocks."""

    day: date
    posters: tuple[DigestFilm, ...]
    blocks: tuple[DigestBlock, ...]


@dataclass(frozen=True)
class DigestWeek:
    """The weekly's timeline (FB-19): no day, one poster strip over the week's films, and the
    blocks, whose rows are **entries** — one film or entity across the whole week under each
    section and update type it touched."""

    posters: tuple[DigestFilm, ...]
    blocks: tuple[DigestBlock, ...]


def group_rows(lines: Iterable[DigestLine], *, order: LineOrder) -> tuple[DigestRow, ...]:
    """One block's lines (of one section) as its rows. Title lines make one film row per film,
    ordered by `film_row_key`, beats by `order.film_row`; entity lines one entity row per
    entity, ordered by name (`entity_row_key`), beats by `order.entity_row` (FB-6). A block
    holds one kind or the other, never both. Over one day's lines a row is a timeline row;
    over a week's, an entry."""
    by_reach: dict[tuple[str, str], tuple[DigestReach | None, list[DigestBeat]]] = {}
    for line in lines:
        key = (
            ("title", str(line.beat.film.film_id))
            if line.reach is None
            else (line.reach.entity_type, line.reach.entity_id)
        )
        by_reach.setdefault(key, (line.reach, []))[1].append(line.beat)
    rows = [
        DigestRow(
            reach=reach,
            beats=tuple(sorted(beats, key=order.film_row if reach is None else order.entity_row)),
        )
        for reach, beats in by_reach.values()
    ]
    return tuple(sorted(rows, key=_row_key(rows)))


def _row_key(rows: Sequence[DigestRow]) -> Callable[[DigestRow], tuple[Any, ...]]:
    if rows and rows[0].reach is not None:
        return entity_row_key
    return film_row_key


def group_update_types(rows: Sequence[DigestRow], *, block: str) -> tuple[DigestUpdateType, ...]:
    """A Not yet reported section's rows laid out by update type: the block's headings in
    their order, each holding one row per film or entity that changed that way, each row only
    that heading's beats (NR-4, per entity). A row with beats of two types sits under both;
    headings with nothing under them are left out. Rows are re-ordered under each heading by
    their own beats, so a film's significance there is the significance of what is listed."""
    types, of_event = (
        (FILM_UPDATE_TYPES, _FILM_UPDATE_TYPE_OF_EVENT)
        if block == FILMS_BLOCK
        else (ENTITY_UPDATE_TYPES, _ENTITY_UPDATE_TYPE_OF_EVENT)
    )
    split: dict[str, list[DigestRow]] = {}
    for row in rows:
        by_type: dict[str, list[DigestBeat]] = {}
        for beat in row.beats:
            by_type.setdefault(of_event.get(beat.event_type, OTHER_UPDATES), []).append(beat)
        for key, beats in by_type.items():
            split.setdefault(key, []).append(DigestRow(reach=row.reach, beats=tuple(beats)))
    return tuple(
        DigestUpdateType(
            key=key, label=label, rows=tuple(sorted(split[key], key=_row_key(split[key])))
        )
        for key, label in types
        if key in split
    )


def group_blocks(lines: Iterable[DigestLine], *, order: LineOrder) -> tuple[DigestBlock, ...]:
    """One day's (or the week's) lines as follow blocks in `FOLLOW_BLOCKS` order, each split
    into In the news and Not yet reported (FB-1). A block or a section with nothing in it is
    left out."""
    by_block: dict[str, list[DigestLine]] = {}
    for line in lines:
        by_block.setdefault(line.block, []).append(line)
    blocks: list[DigestBlock] = []
    for key, label in FOLLOW_BLOCKS:
        if key not in by_block:
            continue
        sections: list[DigestSection] = []
        for news_backed in (True, False):
            rows = group_rows(
                (line for line in by_block[key] if line.beat.news_backed is news_backed),
                order=order,
            )
            if not rows:
                continue
            if news_backed:
                sections.append(DigestSection(news_backed=True, rows=rows))
            else:
                sections.append(
                    DigestSection(
                        news_backed=False, update_types=group_update_types(rows, block=key)
                    )
                )
        blocks.append(DigestBlock(key=key, label=label, sections=tuple(sections)))
    return tuple(blocks)


def day_posters(blocks: Iterable[DigestBlock]) -> tuple[DigestFilm, ...]:
    """The poster strip over every block of a day (FB-7) — or of the week (FB-19): each film
    with a poster once, at its first appearance in reading order (NEU-1533), at most
    `MAX_DAY_POSTERS` — the feed's `dayPosterLeads`. Reading order is the template's: block by
    block, In the news before Not yet reported, update type by update type, row by row, and an
    entity row beat by beat, each beat naming its own film. The strip has no order of its own,
    so whatever reorders the blocks reorders it; a film reached twice, or on two days of the
    week, is one poster where it is first met."""
    posters: dict[UUID, DigestFilm] = {}
    for block in blocks:
        for section in block.sections:
            rows = section.rows or tuple(
                row for update_type in section.update_types for row in update_type.rows
            )
            for row in rows:
                for beat in row.beats:
                    if beat.film.poster_url is not None:
                        posters.setdefault(beat.film.film_id, beat.film)
                    if len(posters) == MAX_DAY_POSTERS:
                        return tuple(posters.values())
    return tuple(posters.values())


def group_days(lines: Iterable[DigestLine]) -> tuple[DigestDay, ...]:
    """The daily's timeline (FB-18): one day per publication day, newest first, each laid out
    as the timeline lays that day out, its rows' lines in the feed's order (`DAY_LINE_ORDER`)."""
    by_day: dict[date, list[DigestLine]] = {}
    for line in lines:
        by_day.setdefault(line.beat.day, []).append(line)
    days: list[DigestDay] = []
    for day, day_lines in sorted(by_day.items(), reverse=True):
        blocks = group_blocks(day_lines, order=DAY_LINE_ORDER)
        days.append(DigestDay(day=day, posters=day_posters(blocks), blocks=blocks))
    return tuple(days)


def group_week(lines: Sequence[DigestLine]) -> DigestWeek:
    """The weekly's timeline (FB-19): the daily's blocks, sections and update types, but read
    by entry rather than by day — one film entry per film under Films and one entity entry per
    entity under the other three, across every day the batch spans, under each section and
    update type it touched. A film with cards in both sections is an entry in both, as it
    would be on two feed days. Film entries rank by their most significant beat, then title
    (DC-3, `film_row_key`); entity entries by name (FB-6). An entry's lines run in publication
    order (`ENTRY_LINE_ORDER`), and the template dates each one. One poster strip, over the
    week's films, each at its first entry."""
    blocks = group_blocks(lines, order=ENTRY_LINE_ORDER)
    return DigestWeek(posters=day_posters(blocks), blocks=blocks)


@dataclass(frozen=True)
class RankedFilm:
    film: DigestFilm
    lead_type: str

    @property
    def lead_label(self) -> str:
        return digest_beat_label(self.lead_type)


def rank_films(beats: Iterable[DigestBeat]) -> tuple[RankedFilm, ...]:
    """Every distinct film in the mail, by its most significant beat on the film arc, then
    casefolded title, then `tmdb_id` (DC-7, FB-22). Over every beat of every reach, so a film
    reached only through a studio can lead. The first is the **lead film**."""
    by_film: dict[UUID, tuple[DigestFilm, list[str]]] = {}
    for beat in beats:
        by_film.setdefault(beat.film.film_id, (beat.film, []))[1].append(beat.event_type)
    ranked = [
        RankedFilm(film=film, lead_type=most_significant_event_type(types))
        for film, types in by_film.values()
    ]
    return tuple(
        sorted(
            ranked,
            key=lambda r: (-event_stage_rank(r.lead_type), r.film.title.casefold(), r.film.tmdb_id),
        )
    )


@dataclass(frozen=True)
class SlateItem:
    """One my-films calendar row inside the slate window — a (film, release type) with a US
    date — and its marker (FB-26)."""

    calendar: CalendarItem
    """The row exactly as `GET /me/calendar` serves it: the slate shows these fields and no
    others, so it cannot describe a film differently from the calendar page."""
    marker: SlateMarker | None = None
    """`new` or `moved` when the date was set or moved since the previous slate day (DC-9);
    None for a date that has not changed, which carries nothing."""


@dataclass(frozen=True)
class SlateBucket:
    """One release type's rows on one slate date, under the calendar's sub-heading."""

    bucket: str
    items: tuple[SlateItem, ...]

    @property
    def label(self) -> str:
        """The calendar's bucket label (`components/calendar/release-labels.ts`): the date
        heading above it says "release", so the bucket does not."""
        return RELEASE_BUCKET_LABELS.get(self.bucket, self.bucket.title())


@dataclass(frozen=True)
class SlateDay:
    """One date on the slate and everything the user's followed films have on it, by bucket."""

    day: date
    buckets: tuple[SlateBucket, ...]

    @property
    def count(self) -> int:
        return sum(len(bucket.items) for bucket in self.buckets)


@dataclass(frozen=True)
class SlateMonth:
    """The slate's dates in one month, under a month heading only when the slate spans more
    than one (FB-26)."""

    heading: str | None
    days: tuple[SlateDay, ...]


def _bucket_rank(bucket: str) -> int:
    """`_calendar_type_rank`'s order in Python: wide, limited, digital; a bucket
    nobody ranked last, as the calendar sorts it."""
    if bucket in _CALENDAR_BUCKET_ORDER:
        return _CALENDAR_BUCKET_ORDER.index(bucket)
    return len(_CALENDAR_BUCKET_ORDER)


def group_slate(items: Iterable[SlateItem]) -> tuple[SlateDay, ...]:
    """The calendar's date → bucket nesting (`lib/calendar-groups.ts::groupByReleaseDate`):
    dates soonest first, buckets in the calendar's order, rows in the order given — which is
    `_calendar_page`'s, so within a bucket the slate orders films as the calendar page does.
    Pure."""
    by_day: dict[date, dict[str, list[SlateItem]]] = {}
    for item in items:
        by_day.setdefault(item.calendar.release_date, {}).setdefault(
            item.calendar.release_type, []
        ).append(item)
    return tuple(
        SlateDay(
            day=day,
            buckets=tuple(
                SlateBucket(bucket=bucket, items=tuple(rows))
                for bucket, rows in sorted(buckets.items(), key=lambda kv: _bucket_rank(kv[0]))
            ),
        )
        for day, buckets in sorted(by_day.items())
    )


def slate_months(days: Sequence[SlateDay]) -> tuple[SlateMonth, ...]:
    """The slate's days under month headings — the calendar's month level, which a 30-day
    window only needs when it crosses a month boundary. One heading-less group when every date
    is in one month (the long date headings already name it); otherwise a group per month,
    each headed by the month's name, as the calendar heads its months. No year level: a
    30-day window that crosses one is still read by its months. Pure."""
    groups: list[tuple[tuple[int, int], list[SlateDay]]] = []
    for day in days:
        key = (day.day.year, day.day.month)
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(day)
    if len(groups) <= 1:
        return tuple(SlateMonth(heading=None, days=tuple(g)) for _, g in groups)
    return tuple(SlateMonth(heading=_MONTHS[month - 1], days=tuple(g)) for (_, month), g in groups)


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
    lines: tuple[DigestLine, ...]
    """Every queued card once per reach that delivered it, unordered — `group_days` (daily) or
    `group_week` (weekly) lays them out, `rank_films` ranks them. Nothing is capped (FB-21)."""
    unsendable: tuple[tuple[UUID, str], ...]
    """`(notification id, why)` for rows this pass can never send — the event lost its summary,
    is no longer the published card, or no follow reaches it any more — marked `failed` with
    the reason, so a permanently un-sendable row cannot stall silently in a backlog read
    nightly."""
    slate: tuple[SlateDay, ...] = ()

    @property
    def beats(self) -> tuple[DigestBeat, ...]:
        """Each card once, however many reaches delivered it."""
        return tuple({line.beat.notification_id: line.beat for line in self.lines}.values())

    @property
    def item_ids(self) -> list[UUID]:
        """Every card's row: all of them are marked `sent`, because every one renders."""
        return [beat.notification_id for beat in self.beats]

    @property
    def slate_count(self) -> int:
        return sum(d.count for d in self.slate)

    @property
    def has_content(self) -> bool:
        return bool(self.lines) or bool(self.slate)


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


async def load_lines(
    session: AsyncSession, *, user_id: UUID, settings: Settings
) -> tuple[tuple[DigestLine, ...], tuple[tuple[UUID, str], ...]]:
    """This user's `queued` digest rows as lines — one per (card, reach) — plus the rows that
    can never be sent.

    The join is the mail's beat half — the film, the event's type, confidence, summary and
    whether a story covers it (the feed's section split, `_has_story`) — and the event's status
    and summary are re-read rather than trusted from the queue, because the queue outlives the
    run that wrote it. `EventSummary` is the one outer join so a missing summary comes back as
    a row to fail rather than a row that quietly disappears.

    Then one batched lookup per film-header input, one for the sources and one for the reaches
    (`_load_reaches`), each over the whole batch rather than per line: the parenthetical's
    directors and countries come from the helpers the feed uses, so the mail cannot describe a
    film differently from the site."""
    rows = await session.execute(
        select(
            Notification.id,
            Event.id.label("event_id"),
            Event.event_type,
            Event.status.label("event_status"),
            Event.confidence,
            Event.created_at,
            Event.occurred_at,
            _has_story().label("has_story"),
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
    sendable: list[Row[Any]] = []
    for row in rows:
        if row.event_status != "published":
            unsendable.append((row.id, "the event is no longer published"))
        elif row.summary is None:
            unsendable.append((row.id, "the event has no summary"))
        else:
            sendable.append(row)
    if not sendable:
        return (), tuple(unsendable)

    reaches = await _load_reaches(
        session, user_id=user_id, event_ids=[row.event_id for row in sendable], settings=settings
    )
    film_ids = {row.film_id for row in sendable}
    directors = await _directors_for_films(session, film_ids)
    countries = await _production_countries_for_films(session, film_ids)
    sources = await _first_sources(session, [row.event_id for row in sendable if row.has_story])

    films: dict[UUID, DigestFilm] = {}
    lines: list[DigestLine] = []
    for row in sendable:
        reached = reaches.get(row.event_id)
        if not reached:
            unsendable.append((row.id, "no follow reaches the event any more"))
            continue
        film = films.get(row.film_id)
        if film is None:
            film = films[row.film_id] = DigestFilm(
                film_id=row.film_id,
                tmdb_id=row.tmdb_id,
                title=row.title,
                film_url=film_url(row.tmdb_id, row.title, settings.public_base_url),
                poster_url=poster_url(row.poster_path, settings.tmdb_image_base),
                parenthetical=film_parenthetical(
                    production_countries=countries.get(row.film_id, []),
                    directors=directors.get(row.film_id, []),
                    release_year=_release_year(row.release_date),
                    arc_stage=derive_arc_stage(row.film_status),
                ),
            )
        beat = DigestBeat(
            notification_id=row.id,
            event_id=row.event_id,
            event_type=row.event_type,
            created_at=row.created_at,
            occurred_at=row.occurred_at,
            confidence=row.confidence,
            summary=row.summary,
            source=sources.get(row.event_id),
            news_backed=row.has_story,
            film=film,
        )
        lines.extend(DigestLine(beat=beat, reach=reach) for reach in reached)
    return tuple(lines), tuple(unsendable)


async def _first_sources(session: AsyncSession, event_ids: list[UUID]) -> dict[UUID, DigestSource]:
    """Each story-covered card's first source in the order `EventOut.sources` lists them —
    newest distinct outlet first (`cap_sources`) — named by outlet and linked (DC-5). A card
    with no story row left is absent, and gets no source line."""
    first: dict[UUID, DigestSource] = {}
    for event_id, stories in (await _sources_by_event(session, event_ids)).items():
        capped = cap_sources(stories)
        if capped:
            first[event_id] = DigestSource(name=outlet_label(capped[0]), url=source_url(capped[0]))
    return first


async def _load_reaches(
    session: AsyncSession,
    *,
    user_id: UUID,
    event_ids: list[UUID],
    settings: Settings,
) -> dict[UUID, tuple[DigestReach | None, ...]]:
    """Per card, every follow of this user's that reached it (FB-25): `follow_attribution_pairs`
    narrowed to the batch's event ids — the same pairs the timeline's rows are built from, so
    the page and the mail cannot disagree — with the title arm as `None` and each entity arm
    named and linked through `entity_names`, one lookup per entity table. An entity the catalog
    cannot name keeps its line under the type's fallback headline (FB-10)."""
    pairs = follow_attribution_pairs(user_id).subquery("pairs")
    rows = (
        await session.execute(
            select(pairs.c.entity_type, pairs.c.entity_id, pairs.c.event_id).where(
                pairs.c.event_id.in_(event_ids)
            )
        )
    ).all()
    names = await entity_names(
        session,
        {(row.entity_type, row.entity_id) for row in rows if row.entity_type != "title"},
    )
    base = settings.public_base_url.rstrip("/")
    reached: dict[UUID, list[DigestReach | None]] = {}
    for entity_type, entity_id, event_id in rows:
        if entity_type == "title":
            reached.setdefault(event_id, []).append(None)
            continue
        resolved = names[(entity_type, entity_id)]
        reached.setdefault(event_id, []).append(
            DigestReach(
                entity_type=entity_type,
                entity_id=entity_id,
                name=None if resolved is None else resolved.name,
                url=(
                    None
                    if resolved is None
                    else f"{base}/{_ENTITY_ROUTES[entity_type]}/{resolved.ref}"
                ),
            )
        )
    return {event_id: tuple(reaches) for event_id, reaches in reached.items()}


async def load_slate(session: AsyncSession, *, user_id: UUID, today: date) -> tuple[SlateDay, ...]:
    """The my-films calendar for the slate window: the upcoming US dates for the films this
    user follows by title, soonest first (D-33), as the calendar page builds them (FB-26).

    Not a query of its own: `public.service._calendar_page` over the my-films governing CTE
    (`title_follow_user_id`) and the my-films cuts (`_my_films_visible`), with the window's end
    added. So the set, the governing-date collapse, the region, the slug rule, the within-date
    order and every field a row shows are the calendar page's — the slate cannot name a date,
    or describe a film, differently from it. The window is `SLATE_WINDOW_DAYS` dates starting
    today: a date that is today is still a date to know about, and the day the count runs out
    is the first one left off. The calendar pages by *date*, so a page of that many dates holds
    the whole window.

    Each row's marker is `load_slate_markers`' answer for its (film, bucket) — the one thing
    the slate shows that the calendar page does not.
    """
    governing = _calendar_governing_cte(name="slate_governing", title_follow_user_id=user_id)
    page = await _calendar_page(
        session,
        governing=governing,
        visible=(
            *_my_films_visible(governing, today=today),
            governing.c.governing_date < today + timedelta(days=SLATE_WINDOW_DAYS),
        ),
        limit=SLATE_WINDOW_DAYS,
        offset=0,
    )
    if not page.items:
        return ()
    # The calendar row addresses its film by ref, which `_calendar_page` mints from the tmdb id;
    # the markers are keyed by the film's own id.
    tmdb_by_ref = {item.film_ref: parse_film_ref(item.film_ref) for item in page.items}
    id_by_tmdb = dict(
        (
            await session.execute(
                select(Film.tmdb_id, Film.id).where(Film.tmdb_id.in_(set(tmdb_by_ref.values())))
            )
        )
        .tuples()
        .all()
    )
    film_id_by_ref = {
        ref: id_by_tmdb[tmdb_id] for ref, tmdb_id in tmdb_by_ref.items() if tmdb_id in id_by_tmdb
    }
    markers = await load_slate_markers(session, film_ids=set(film_id_by_ref.values()), today=today)
    return group_slate(
        SlateItem(
            calendar=item,
            marker=(
                markers.get((film_id, item.release_type))
                if (film_id := film_id_by_ref.get(item.film_ref)) is not None
                else None
            ),
        )
        for item in page.items
    )


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
    tokens = [f"{PRIMARY_REGION}:{bucket}" for bucket in _CALENDAR_BUCKET_ORDER]
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
    lines, unsendable = await load_lines(session, user_id=recipient.user_id, settings=settings)
    if include_slate is None:
        include_slate = carries_slate(cadence, today, settings) and recipient.deliverable
    slate: tuple[SlateDay, ...] = ()
    if include_slate:
        slate = await load_slate(session, user_id=recipient.user_id, today=today)
    return DigestBatch(recipient=recipient, lines=lines, unsendable=unsendable, slate=slate)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _headline(ranked: RankedFilm) -> str:
    """ "Heat 2 — casting": a film as the subject and the preheader name it."""
    return f"{ranked.film.title} — {ranked.lead_label.lower()}"


def digest_subject(batch: DigestBatch) -> str:
    """The subject line (DC-7, FB-22): the lead film and its lead beat, how many more films,
    and whether the slate is in — or, for a slate alone, how many dates. `N` counts the other
    distinct **films** across every reach — a film under two entity rows is one film — never
    rows and never beats.

    Raises on an empty batch: `render_batch` never builds one, and a caller that skipped that
    check should fail loudly rather than send a subject about nothing."""
    ranked = rank_films(batch.beats)
    if not ranked:
        if not batch.slate:
            raise ValueError("a digest with no lines and no slate has no subject")
        return f"Your slate: {_plural(batch.slate_count, 'upcoming date')}"
    subject = _headline(ranked[0])
    more = len(ranked) - 1
    if more:
        subject += f", + {_plural(more, 'more film')}"
    if batch.slate:
        subject += " · your slate"
    return subject


def digest_preheader(batch: DigestBatch) -> str:
    """The inbox preview text (DC-10): what the subject had no room for — the slate's size,
    then up to two films after the lead, by the subject's ranking. Empty when there is nothing
    to add, and the template then omits the element rather than rendering it empty."""
    parts: list[str] = []
    if batch.slate:
        parts.append(
            f"Your slate: {_plural(batch.slate_count, 'date')} in the next "
            f"{SLATE_WINDOW_DAYS} days."
        )
    also = rank_films(batch.beats)[1:3]
    if also:
        parts.append("Also: " + "; ".join(_headline(ranked) for ranked in also))
    return " ".join(parts)


def _film_context(film: DigestFilm) -> dict[str, object]:
    return {"title": film.title, "parenthetical": film.parenthetical, "url": film.film_url}


def _line_context(
    beat: DigestBeat,
    *,
    names_film: bool,
    show_label: bool,
    show_marker: bool,
    with_dates: bool,
    today: date,
) -> dict[str, object]:
    """One line as the template renders it (FB-20). `prefix` is the bold lead — the date
    (weekly only: the daily's day heading says it) and the beat label where the heading above
    does not already name the type. `film` is set on an entity row's lines, which name their
    film; a film row's lines do not, the row is headed by it."""
    prefix = [short_date(beat.day, today=today)] if with_dates else []
    if show_label:
        prefix.append(beat.label)
    return {
        "prefix": " · ".join(prefix) or None,
        "unconfirmed": show_marker and beat.confidence == "rumored",
        "film": _film_context(beat.film) if names_film else None,
        "summary": beat.summary,
        "source": (
            {"name": beat.source.name, "url": beat.source.url} if beat.source is not None else None
        ),
    }


def _row_context(
    row: DigestRow, *, news_backed: bool, show_label: bool, with_dates: bool, today: date
) -> dict[str, object]:
    """A film row (headed by the film, linked) or an entity row (headed by the entity, linked
    when it has a page). Under In the news a film row credits JustWatch after its lines when
    one of them is `now_available` (DC-17); under Not yet reported the Now available heading
    does, once (NR-8), and an entity row never does (FB-3)."""
    lines = [
        _line_context(
            beat,
            names_film=row.reach is not None,
            show_label=show_label,
            # Not yet reported's heading carries "(unconfirmed)" for the whole section (NR-5).
            show_marker=news_backed,
            with_dates=with_dates,
            today=today,
        )
        for beat in row.beats
    ]
    if row.reach is None:
        return {
            "film": _film_context(row.film),
            "entity": None,
            "lines": lines,
            "credits_justwatch": news_backed and row.credits_justwatch,
        }
    return {
        "film": None,
        "entity": {"name": row.reach.headline, "url": row.reach.url},
        "lines": lines,
        "credits_justwatch": False,
    }


def _section_context(
    section: DigestSection, *, block: str, with_dates: bool, today: date
) -> dict[str, object]:
    if section.news_backed:
        return {
            "label": IN_THE_NEWS_LABEL,
            "qualifier": None,
            "rows": [
                _row_context(
                    row, news_backed=True, show_label=True, with_dates=with_dates, today=today
                )
                for row in section.rows
            ],
            "update_types": [],
        }
    return {
        "label": NOT_YET_REPORTED_LABEL,
        "qualifier": NOT_YET_REPORTED_QUALIFIER,
        "rows": [],
        "update_types": [
            {
                "label": update_type.label,
                "rows": [
                    _row_context(
                        row,
                        news_backed=False,
                        # A named heading says the type; Other updates names nothing (NR-5).
                        show_label=update_type.key == OTHER_UPDATES,
                        with_dates=with_dates,
                        today=today,
                    )
                    for row in update_type.rows
                ],
                "credits_justwatch": block == FILMS_BLOCK and update_type.key == "now_available",
            }
            for update_type in section.update_types
        ],
    }


def _posters_context(posters: Sequence[DigestFilm]) -> list[dict[str, object]]:
    return [
        {"title": film.title, "url": film.film_url, "poster_url": film.poster_url}
        for film in posters
    ]


def _blocks_context(
    blocks: Sequence[DigestBlock], *, with_dates: bool, today: date
) -> list[dict[str, object]]:
    return [
        {
            "label": block.label,
            "sections": [
                _section_context(section, block=block.key, with_dates=with_dates, today=today)
                for section in block.sections
            ],
        }
        for block in blocks
    ]


def _timeline_context(
    lines: Sequence[DigestLine], *, cadence: DigestCadence, today: date
) -> dict[str, object]:
    """The "New on your timeline" half of the context: `days` for the daily (FB-18), `week`
    for the weekly (FB-19), the other empty. A line carries its date exactly when no day
    heading above it does — so in the weekly, always."""
    if cadence == "daily":
        return {
            "days": [
                {
                    "heading": day_heading(d.day),
                    "posters": _posters_context(d.posters),
                    "blocks": _blocks_context(d.blocks, with_dates=False, today=today),
                }
                for d in group_days(lines)
            ],
            "week": None,
        }
    if not lines:
        return {"days": [], "week": None}
    week = group_week(lines)
    return {
        "days": [],
        "week": {
            "posters": _posters_context(week.posters),
            "blocks": _blocks_context(week.blocks, with_dates=True, today=today),
        },
    }


def _slate_context(days: Sequence[SlateDay], *, settings: Settings) -> list[dict[str, object]]:
    """The slate half of the context (FB-26): month (headed only across a boundary) → date →
    bucket → the calendar's film row, its fields as `CalendarFilmRow` shows them, plus the
    marker. `films`, not `items`: Jinja resolves `bucket.items` to the dict method."""
    return [
        {
            "heading": month.heading,
            "days": [
                {
                    "heading": day_heading(d.day),
                    "buckets": [
                        {
                            "label": bucket.label,
                            "films": [
                                {
                                    "title": item.calendar.film_title,
                                    "year": item.calendar.release_year,
                                    "url": ref_url(
                                        item.calendar.film_ref, settings.public_base_url
                                    ),
                                    "poster_url": poster_url(
                                        item.calendar.poster_path,
                                        settings.tmdb_image_base,
                                        size=SLATE_POSTER_SIZE,
                                    ),
                                    "director": item.calendar.director,
                                    "stars": " · ".join(item.calendar.stars),
                                    "genres": " · ".join(item.calendar.genres),
                                    "marker": item.marker,
                                }
                                for item in bucket.items
                            ],
                        }
                        for bucket in d.buckets
                    ],
                }
                for d in month.days
            ],
        }
        for month in slate_months(days)
    ]


def digest_context(
    batch: DigestBatch, *, cadence: DigestCadence, today: date, settings: Settings
) -> dict[str, object]:
    """The `digest` template's context for one batch — the only place it is built. Every
    string the mail shows is decided here; the templates lay it out and compute nothing.

    The daily is laid out by day (FB-18); the weekly by entry, its lines dated (FB-19).

    Raises `ValueError` (through `digest_subject`) for a batch with nothing to say:
    `render_batch` never builds one, so a caller that does has skipped that check."""
    return {
        "product_name": settings.product_name,
        "display_name": batch.recipient.display_name,
        "settings_url": settings_url(settings.public_base_url),
        "cadence": cadence,
        "slate_window_days": SLATE_WINDOW_DAYS,
        "subject": digest_subject(batch),
        "preheader": digest_preheader(batch),
        "slate": _slate_context(batch.slate, settings=settings),
        **_timeline_context(batch.lines, cadence=cadence, today=today),
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
        reply_to=settings.mail_reply_to or None,
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
    Every row the mail carried is marked `sent`: nothing is cut (FB-21)."""
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
