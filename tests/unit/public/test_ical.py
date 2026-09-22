"""The hand-rolled RFC 5545 writer (D-34).

Every structural assertion here goes through `icalendar`, a third-party parser (dev-only
dependency), rather than through string matching on the output. That is the point of the test:
the writer is hand-rolled, so what needs proving is that something *other than its author* reads
it the way the author intended. The few raw-text assertions that remain are for properties a
parser deliberately normalises away — CRLF endings and line folding — which a parser-only test
would pass while the output was unusable.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from icalendar import Calendar
from icalendar.cal import Component

from tests.fixtures.ical import events, prop_dt, prop_text
from upmovies.public.ical import CalendarFeedEvent, render_calendar

BASE_URL = "https://backlotter.test"


def _event(**kwargs) -> CalendarFeedEvent:
    defaults = {
        "film_id": "11111111-1111-1111-1111-111111111111",
        "bucket": "wide",
        "title": "A Film",
        "release_date": date(2026, 12, 5),
        "film_ref": "1001-a-film",
        "updated_at": datetime(2026, 9, 18, 14, 30, 0, tzinfo=UTC),
    }
    return CalendarFeedEvent(**{**defaults, **kwargs})


def _render(feed: list[CalendarFeedEvent]) -> str:
    return render_calendar(feed, base_url=BASE_URL)


def _only(feed: list[CalendarFeedEvent]) -> Component:
    (vevent,) = events(_render(feed))
    return vevent


# --- the envelope --------------------------------------------------------------------------


def test_the_envelope_parses_and_carries_the_required_properties():
    cal = Calendar.from_ical(_render([_event()]))

    assert cal["version"] == "2.0"
    assert cal["prodid"] == "-//backlotter//calendar//EN"
    assert cal["calscale"] == "GREGORIAN"
    assert str(cal["x-wr-calname"]) == "backlotter — your films"


def test_an_empty_feed_is_still_a_parseable_calendar():
    # A subscriber who follows no films must get a feed their client keeps polling, not a
    # refusal — see `render_calendar`.
    cal = Calendar.from_ical(_render([]))

    assert cal.walk("VEVENT") == []
    assert cal["prodid"] == "-//backlotter//calendar//EN"


def test_every_line_ends_crlf_and_the_document_does_too():
    out = _render([_event()])

    assert out.endswith("END:VCALENDAR\r\n")
    assert "\n" not in out.replace("\r\n", "")


# --- one event -----------------------------------------------------------------------------


def test_an_event_is_an_all_day_vevent_with_an_exclusive_dtend():
    vevent = _only([_event(release_date=date(2026, 12, 5))])

    # A `date` rather than a `datetime` is how the parser reports VALUE=DATE, i.e. the event is
    # all-day rather than midnight-to-midnight in some timezone.
    assert prop_dt(vevent, "dtstart") == date(2026, 12, 5)
    assert prop_dt(vevent, "dtend") == date(2026, 12, 6)
    assert vevent["transp"] == "TRANSPARENT"


def test_the_uid_is_the_film_id_and_the_bucket():
    vevent = _only([_event(film_id="abc-123", bucket="digital")])

    assert prop_text(vevent, "uid") == "abc-123-digital@backlotter"


def test_dtstamp_is_the_dates_own_timestamp_not_the_render_time():
    moved = datetime(2026, 9, 18, 14, 30, 0, tzinfo=UTC)

    assert prop_dt(_only([_event(updated_at=moved)]), "dtstamp") == moved


def test_dtstamp_is_converted_to_utc():
    # 07:30 at UTC-7 is 14:30Z. A DTSTAMP left in a local offset is legal iCalendar but not the
    # UTC form §3.3.5 asks for, and clients differ on reading it.
    local = datetime(2026, 9, 18, 7, 30, 0, tzinfo=timezone(timedelta(hours=-7)))

    assert prop_dt(_only([_event(updated_at=local)]), "dtstamp") == datetime(
        2026, 9, 18, 14, 30, 0, tzinfo=UTC
    )


def test_the_description_and_url_point_at_the_films_page():
    vevent = _only([_event(film_ref="1001-a-film")])

    assert prop_text(vevent, "description") == f"{BASE_URL}/film/1001-a-film"
    assert prop_text(vevent, "url") == f"{BASE_URL}/film/1001-a-film"


def test_the_url_property_is_not_text_escaped():
    # URL is typed URI (§3.3.13), not TEXT: escaping it would leave a client stripping
    # backslashes out of the address. DESCRIPTION, which *is* TEXT, still escapes.
    out = render_calendar([_event(film_ref="1001-a,film;odd")], base_url=BASE_URL)

    assert f"URL:{BASE_URL}/film/1001-a,film;odd" in out
    assert f"DESCRIPTION:{BASE_URL}/film/1001-a\\,film\\;odd" in out


def test_a_trailing_slash_on_the_base_url_does_not_double():
    out = render_calendar([_event(film_ref="1001-a-film")], base_url=f"{BASE_URL}/")

    assert f"{BASE_URL}/film/1001-a-film" in out
    assert "//film/" not in out


# --- the summary ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("wide", "A Film — in theaters"),
        ("limited", "A Film — in theaters (limited)"),
        ("digital", "A Film — digital"),
        ("physical", "A Film — physical"),
    ],
)
def test_the_summary_names_the_bucket(bucket, expected):
    assert prop_text(_only([_event(bucket=bucket)]), "summary") == expected


def test_an_unranked_bucket_falls_back_to_its_own_name():
    # A displayable type added later must read plainly rather than KeyError the whole feed.
    assert prop_text(_only([_event(bucket="premiere")]), "summary") == "A Film — premiere"


# --- escaping and folding ------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Crouching Tiger, Hidden Dragon",  # a comma would make SUMMARY a two-value list
        "Face/Off",
        "Dune: Part Two",
        "Mission; Impossible",
        r"Back\Slash",
        "Line\nBreak",
    ],
)
def test_a_title_round_trips_through_the_parser_unchanged(title):
    assert prop_text(_only([_event(title=title)]), "summary") == f"{title} — in theaters"


def test_a_long_summary_is_folded_and_still_round_trips():
    title = "The Assassination of Jesse James by the Coward Robert Ford, Revisited Again"
    out = _render([_event(title=title)])

    # Folded: the SUMMARY is longer than one content line, so it must be continued rather than
    # emitted whole.
    assert "\r\n " in out
    assert all(len(line.encode()) <= 75 for line in out.split("\r\n"))

    (vevent,) = events(out)
    assert prop_text(vevent, "summary") == f"{title} — in theaters"


def test_folding_never_splits_a_multibyte_character():
    # 40 three-octet characters: a folder counting characters instead of octets cuts one of
    # these in half and the client gets a decode error rather than a title.
    title = "映" * 40
    out = _render([_event(title=title)])

    assert all(len(line.encode()) <= 75 for line in out.split("\r\n"))

    (vevent,) = events(out)
    assert prop_text(vevent, "summary") == f"{title} — in theaters"


# --- several events ------------------------------------------------------------------------


def test_events_are_emitted_in_the_order_given():
    feed = events(
        _render(
            [
                _event(bucket="wide", release_date=date(2026, 12, 5)),
                _event(bucket="digital", release_date=date(2027, 2, 9)),
            ]
        )
    )

    assert [prop_text(v, "uid").rsplit("-", 1)[-1] for v in feed] == [
        "wide@backlotter",
        "digital@backlotter",
    ]
