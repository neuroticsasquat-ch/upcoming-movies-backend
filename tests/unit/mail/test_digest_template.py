"""The `digest` template's copy (NEU-1381): what the daily and weekly digests say, and the
shapes they render — a slate with updates, a slate alone, updates alone, on either cadence.

Beside `test_alert_template.py` for its reason: `test_templates.py` asserts the rendering
rules against whichever template is handy; this asserts the digest's own copy."""

import pytest

from upmovies.mail import MailError, render

SETTINGS_URL = "https://app.example.com/settings"

SLATE = [
    {
        "heading": "Friday, September 25, 2026",
        "entries": [
            {
                "title": "Dune: Part Three",
                "release_label": "Wide release",
                "film_url": "https://app.example.com/film/1234-dune-part-three",
                "poster_url": "https://image.tmdb.org/t/p/w154/dune.jpg",
            }
        ],
    },
    {
        "heading": "Tuesday, October 6, 2026",
        "entries": [
            {
                "title": "Heat 2",
                "release_label": "Digital release",
                "film_url": "https://app.example.com/film/5678-heat-2",
                "poster_url": None,
            }
        ],
    },
]

DAYS = [
    {
        "heading": "Thursday, September 17, 2026",
        "films": [
            {
                "title": "Heat 2",
                "film_url": "https://app.example.com/film/5678-heat-2",
                "poster_url": None,
                "events": [
                    {"beat": "Casting", "summary": "Someone joined the cast."},
                    {"beat": "Production started", "summary": "Cameras are rolling."},
                ],
            }
        ],
    },
    {
        "heading": "Wednesday, September 16, 2026",
        "films": [
            {
                "title": "Dune: Part Three",
                "film_url": "https://app.example.com/film/1234-dune-part-three",
                "poster_url": "https://image.tmdb.org/t/p/w154/dune.jpg",
                "events": [{"beat": "New trailer", "summary": "A trailer landed."}],
            }
        ],
    },
]


def _count(days):
    return sum(len(f["events"]) for d in days for f in d["films"])


def _digest(*, slate=(), days=(), cadence="weekly", **overrides):
    slate, days = list(slate), list(days)
    context: dict[str, object] = {
        "product_name": "Backlotter",
        "display_name": "Ada",
        "settings_url": SETTINGS_URL,
        "cadence": cadence,
        "slate_window_days": 30,
        "slate": slate,
        "days": days,
        "update_count": _count(days),
        "slate_count": sum(len(d["entries"]) for d in slate),
        **overrides,
    }
    return render(
        "digest", context, sender="Backlotter <no-reply@example.com>", to="ada@example.com"
    )


def test_a_weekly_digest_with_a_slate_and_updates_counts_both_in_the_subject():
    envelope = _digest(slate=SLATE, days=DAYS)

    assert envelope.subject == "Your slate: 2 upcoming dates and 3 updates"


def test_a_slate_alone_is_a_slate_mail():
    envelope = _digest(slate=SLATE[:1])

    assert envelope.subject == "Your slate: 1 upcoming date"
    assert "Dune: Part Three" in envelope.text
    assert "New on your timeline" not in envelope.html


def test_updates_alone_name_the_cadence():
    assert _digest(days=DAYS[1:], cadence="weekly").subject == "Your weekly digest: 1 update"
    assert _digest(days=DAYS, cadence="daily").subject == "Your daily digest: 3 updates"


def test_the_slate_lists_every_date_film_and_release_kind_in_both_parts():
    envelope = _digest(slate=SLATE)

    for part in (envelope.text, envelope.html):
        assert "30 days" in part
        for day in SLATE:
            assert day["heading"] in part
            for item in day["entries"]:
                assert item["title"] in part
                assert item["release_label"] in part
                assert item["film_url"] in part


def test_the_timeline_is_grouped_by_day_then_film_with_every_event_under_its_film():
    """The feed's shape, in a mail: the day heading comes before its film, the film before
    its events, and the newer day (first in the list) before the older one."""
    envelope = _digest(days=DAYS)

    for part in (envelope.text, envelope.html):
        newer, older = DAYS
        assert part.index(newer["heading"]) < part.index(older["heading"])
        assert part.index(newer["heading"]) < part.index("Heat 2")
        assert part.index("Heat 2") < part.index("Someone joined the cast.")
        assert part.index("Someone joined the cast.") < part.index("Cameras are rolling.")
        assert part.index("Cameras are rolling.") < part.index(older["heading"])
        for day in DAYS:
            for film in day["films"]:
                assert film["film_url"] in part
                for event in film["events"]:
                    assert event["beat"] in part
                    assert event["summary"] in part


def test_the_slate_comes_before_the_timeline():
    """D-33: the weekly send *is* the slate mail, so the slate leads."""
    envelope = _digest(slate=SLATE, days=DAYS)

    for part in (envelope.text.lower(), envelope.html.lower()):
        assert part.index("your slate") < part.index("on your timeline")


def test_the_text_part_carries_links_as_bare_urls():
    envelope = _digest(slate=SLATE, days=DAYS)

    assert "<a " not in envelope.text
    assert SLATE[0]["entries"][0]["film_url"] in envelope.text
    assert DAYS[0]["films"][0]["film_url"] in envelope.text


def test_the_poster_is_rendered_when_there_is_one_and_omitted_when_there_is_not():
    with_poster = _digest(days=DAYS[1:]).html
    without = _digest(days=DAYS[:1]).html

    assert "<img" in with_poster
    assert DAYS[1]["films"][0]["poster_url"] in with_poster
    assert "<img" not in without


def test_both_parts_carry_the_settings_link_and_say_why_the_mail_arrived():
    envelope = _digest(days=DAYS)

    for part in (envelope.text, envelope.html):
        assert SETTINGS_URL in part
        assert "follow" in part
        assert "films you follow" in part
        assert "weekly digest" in part


def test_display_name_is_optional_the_way_every_other_template_makes_it():
    envelope = _digest(days=DAYS, display_name="")

    assert envelope.text.startswith("Hi,")


def test_a_title_with_markup_in_it_escapes_in_html_and_not_in_text():
    day = {**DAYS[0], "films": [{**DAYS[0]["films"][0], "title": "Heat & <Sons>"}]}

    envelope = _digest(days=[day])

    assert "Heat &amp; &lt;Sons&gt;" in envelope.html
    assert "Heat & <Sons>" in envelope.text


def test_a_digest_with_nothing_to_say_is_not_a_mail():
    """Not reachable through `digest_sender.send_batch`, which sends nothing for an empty
    batch — asserted so a future caller that does not gets a loud failure rather than a
    subjectless mail."""
    with pytest.raises(MailError):
        _digest()
