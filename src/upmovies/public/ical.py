"""The iCalendar (RFC 5545) serializer behind `GET /calendar/{token}.ics` (D-34).

Hand-rolled rather than a dependency, for the reason `sitemap.py` next door is: the output is a
fixed set of properties over an all-day VEVENT, the whole writer is the folding and escaping
rules below, and a library would bring a model of recurrence, alarms and timezones that this
feed has no use for. The *test* does use a third-party parser (`icalendar`, dev-only) — a
hand-rolled writer checked by a hand-rolled parser would only prove the two agree about the
spec, which is the one thing not in doubt.

Three rules do the work, and each of them is a real interoperability failure when skipped:

- **CRLF line endings** (§3.1). LF-only output is accepted by some clients and silently
  rejected by others.
- **Folding at 75 octets** (§3.1) — *octets*, not characters, so the split is computed over
  UTF-8 bytes and never inside one. A film title with an accent in it is exactly where a
  character-counting folder produces a line that decodes to mojibake.
- **TEXT escaping** (§3.3.11) for `\\`, `;`, `,` and newlines. Titles carry commas and colons
  routinely (``Face/Off``, ``Dune: Part Two``); an unescaped comma in SUMMARY makes the value a
  *list* of two values, and the second half of the title vanishes from the client.

All-day events use `VALUE=DATE` with an **exclusive** `DTEND` of the following day (§3.8.2.2):
a release on the 5th is `DTSTART:20261205` / `DTEND:20261206`. Omitting DTEND is legal and
means the same thing, but a handful of clients render a zero-length event, so it is spelled out.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

PRODID = "-//backlotter//calendar//EN"
"""Identifies the software that wrote the object (§3.7.3). Free-form, but it must be there."""

CALENDAR_NAME = "backlotter — your films"
"""`X-WR-CALNAME`: the name a client shows for a subscribed feed. Not in RFC 5545 at all, but
it is what Google, Apple and Outlook all read, and the alternative is a calendar named after
its URL."""

MAX_LINE_OCTETS = 75
"""The content line limit (§3.1), excluding the CRLF."""

# What the summary calls each release bucket. Three phrasings for four buckets is deliberate:
# the theatrical arc reads as one thing to a person putting it in their calendar, so both its
# buckets say "in theaters" — but `limited` carries the parenthetical, because a film with both
# dates would otherwise land two events with byte-identical summaries on two different days and
# the user could not tell which was which from the notification alone.
BUCKET_SUMMARY_SUFFIX: dict[str, str] = {
    "wide": "in theaters",
    "limited": "in theaters (limited)",
    "digital": "digital",
    "physical": "physical",
}


@dataclass(frozen=True)
class CalendarFeedEvent:
    """One all-day VEVENT: a film the subscriber follows reaching one release bucket.

    `film_id` and `bucket` are the UID's two halves (D-34), and the UID is what makes a date
    *move* rather than duplicate: the same subject re-published with a new DTSTART updates the
    event already in the user's calendar. Which is why neither half may be anything the film's
    metadata can change — a title-derived id would strand the old event and add a second one
    the first time TMDB corrects a title.
    """

    film_id: str
    bucket: str
    title: str
    release_date: date
    film_ref: str
    updated_at: datetime
    """When this subject's date was last set or moved — DTSTAMP. Deliberately not "now": a feed
    a phone re-fetches hourly must not announce that every event in it changed since the last
    fetch."""


def render_calendar(events: Sequence[CalendarFeedEvent], *, base_url: str) -> str:
    """The whole VCALENDAR document, ready to serve.

    An empty `events` renders the envelope with no components. RFC 5545 §3.4 reads as requiring
    at least one, but the alternative here is worse: a subscriber who follows no films (or none
    with a US date yet) needs their calendar client to keep the subscription and
    poll it again, and every client does that for an empty feed while several drop a
    subscription that 404s.
    """
    base = base_url.rstrip("/")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(CALENDAR_NAME)}",
    ]
    for event in events:
        lines.extend(_render_event(event, base=base))
    lines.append("END:VCALENDAR")
    return "".join(f"{_fold(line)}\r\n" for line in lines)


def _render_event(event: CalendarFeedEvent, *, base: str) -> tuple[str, ...]:
    url = f"{base}/film/{event.film_ref}"
    summary = f"{event.title} — {BUCKET_SUMMARY_SUFFIX.get(event.bucket, event.bucket)}"
    return (
        "BEGIN:VEVENT",
        f"UID:{event.film_id}-{event.bucket}@backlotter",
        f"DTSTAMP:{_utc_stamp(event.updated_at)}",
        f"DTSTART;VALUE=DATE:{_date_value(event.release_date)}",
        f"DTEND;VALUE=DATE:{_date_value(event.release_date + timedelta(days=1))}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(url)}",
        # DESCRIPTION is TEXT and escapes; URL is typed URI (§3.3.13) and must not — TEXT rules
        # would turn a `,` or `;` in a URL into an escape sequence the client then strips.
        f"URL:{url}",
        # A release date occupies the day without making the subscriber busy for it: an
        # opaque all-day event would show them as unavailable to anyone reading their calendar.
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    )


def _escape(value: str) -> str:
    """Escape a TEXT property value (§3.3.11). Backslash first, or it would escape its own
    replacements."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _date_value(value: date) -> str:
    """A DATE value (§3.3.4): `YYYYMMDD`, no separators."""
    return value.strftime("%Y%m%d")


def _utc_stamp(value: datetime) -> str:
    """A UTC DATE-TIME value (§3.3.5): `YYYYMMDDTHHMMSSZ`.

    A naive input is read as UTC rather than rejected: everything this feed reads is a
    `timestamptz`, so the only way to get one is a caller constructing it by hand, and a DTSTAMP
    an hour out is not worth a 500 on a calendar fetch.
    """
    in_utc = value.astimezone(UTC) if value.tzinfo is not None else value
    return in_utc.strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> str:
    """Fold one content line to `MAX_LINE_OCTETS` octets per §3.1.

    The continuation marker is CRLF followed by a single space, and the split is made on UTF-8
    byte boundaries: a multi-byte character cut in half is a decode error at the client, not a
    cosmetic problem. Continuation lines are 74 octets of content plus the leading space, so
    every line on the wire is within the limit.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= MAX_LINE_OCTETS:
        return line
    chunks: list[str] = []
    remaining = encoded
    limit = MAX_LINE_OCTETS
    while remaining:
        cut = min(limit, len(remaining))
        # Back off until the chunk is whole UTF-8: a continuation byte (0b10xxxxxx) at the cut
        # means we are mid-character.
        while cut > 0 and cut < len(remaining) and remaining[cut] & 0xC0 == 0x80:
            cut -= 1
        chunks.append(remaining[:cut].decode("utf-8"))
        remaining = remaining[cut:]
        limit = MAX_LINE_OCTETS - 1  # the continuation space costs one octet
    return "\r\n ".join(chunks)
