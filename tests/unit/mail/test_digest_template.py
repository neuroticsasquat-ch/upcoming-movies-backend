"""The `digest` template's copy (NEU-1381, NEU-1460, NEU-1461, NEU-1462): what the daily and
weekly digests say, laid out as film entries — a header, a "Following:" line, dated and sourced
beats — beside the slate and its new/moved markers, under a wordmark, with the lead film as a
lead card and the rest as compact rows.

Beside `test_alert_template.py` for its reason: `test_templates.py` asserts the rendering
rules against whichever template is handy; this asserts the digest's own copy. The context is
hand-built in the shape `digest_sender.digest_context` builds, because the template computes
nothing: every string it shows arrives here as a value."""

import re

import pytest

from upmovies.mail import MailError, render

SETTINGS_URL = "https://app.example.com/settings"
TIMELINE_URL = "https://app.example.com/"
UNSUBSCRIBE_URL = "https://api.example.com/digest/unsubscribe/tok-ada"

SLATE = [
    {
        "heading": "Friday, September 25, 2026",
        "entries": [
            {
                "title": "Dune: Part Three",
                "release_label": "Wide release",
                "film_url": "https://app.example.com/film/1234-dune-part-three",
                "poster_url": "https://image.tmdb.org/t/p/w154/dune.jpg",
                "marker": None,
            }
        ],
    },
]


def _slate_row(title: str, marker: str | None) -> dict[str, object]:
    slug = title.lower().replace(" ", "-")
    return {
        "title": title,
        "release_label": "Wide release",
        "film_url": f"https://app.example.com/film/1-{slug}",
        "poster_url": None,
        "marker": marker,
    }


MARKED_SLATE = [
    {
        "heading": "Friday, September 25, 2026",
        "entries": [
            _slate_row("Arrival", "new"),
            _slate_row("Blade", "moved"),
            _slate_row("Casino", None),
        ],
    },
]

HEAT = {
    "title": "Heat 2",
    "film_url": "https://app.example.com/film/5678-heat-2",
    "poster_url": None,
    "parenthetical": "USA, Dir: Michael Mann, 2026",
    "status": "Wide release · 14 August 2026",
    "following": [
        {"name": "Michael Mann", "url": "https://app.example.com/person/1-michael-mann"},
        {"name": "Legendary Pictures", "url": "https://app.example.com/studio/2-legendary"},
    ],
    "beats": [
        {
            "date": "22 Sep",
            "label": "Casting",
            "unconfirmed": True,
            "summary": "Ada is in talks.",
            "source": {"name": "Deadline", "url": "https://deadline.example/heat-2-ada"},
        },
        {
            "date": "23 Sep",
            "label": "Production started",
            "unconfirmed": False,
            "summary": "Cameras are rolling.",
            "source": {"name": "TMDB", "url": None},
        },
    ],
    "credits_justwatch": False,
}

ZODIAC = {
    "title": "Zodiac",
    "film_url": "https://app.example.com/film/9-zodiac",
    "poster_url": "https://image.tmdb.org/t/p/w154/zodiac.jpg",
    "parenthetical": "USA, Dir: David Fincher, 2007",
    "status": "Released",
    "following": [],
    "beats": [
        {
            "date": "20 Sep",
            "label": "Now streaming",
            "unconfirmed": False,
            "summary": "Now streaming on Netflix.",
            "source": {"name": "TMDB", "url": None},
        },
        {
            "date": "21 Sep 2025",
            "label": "New trailer",
            "unconfirmed": False,
            "summary": "A trailer landed.",
            "source": None,
        },
    ],
    "credits_justwatch": True,
}

ENTRIES = [HEAT, ZODIAC]


def _digest(
    *,
    slate=(),
    entries=(),
    cadence="weekly",
    subject="Heat 2 — casting, + 1 more film",
    preheader="",
    overflow=0,
    **overrides,
):
    """`entries` in mail order; the first becomes the context's `lead`, as `digest_context`
    splits it."""
    lead, *rest = list(entries) or [None]
    context: dict[str, object] = {
        "product_name": "Backlotter",
        "display_name": "Ada",
        "settings_url": SETTINGS_URL,
        "cadence": cadence,
        "slate_window_days": 30,
        "subject": subject,
        "preheader": preheader,
        "slate": list(slate),
        "lead": lead,
        "entries": rest,
        "overflow": overflow,
        "overflow_line": (
            f"and {overflow} more film{'' if overflow == 1 else 's'} on your timeline"
            if overflow
            else ""
        ),
        "timeline_url": TIMELINE_URL,
        "unsubscribe_url": UNSUBSCRIBE_URL,
        **overrides,
    }
    return render(
        "digest", context, sender="Backlotter <no-reply@example.com>", to="ada@example.com"
    )


def test_the_subject_is_the_contexts_verbatim():
    envelope = _digest(entries=ENTRIES, subject="Heat 2 — casting, + 1 more film · your slate")

    assert envelope.subject == "Heat 2 — casting, + 1 more film · your slate"


def test_both_parts_carry_every_title_header_beat_date_source_and_url():
    envelope = _digest(entries=ENTRIES)

    for part in (envelope.text, envelope.html):
        for entry in ENTRIES:
            assert entry["title"] in part
            assert entry["film_url"] in part
            assert f"({entry['parenthetical']})" in part
            assert entry["status"] in part
            for f in entry["following"]:
                assert f["name"] in part
                assert f["url"] in part
            for beat in entry["beats"]:
                assert beat["date"] in part
                assert beat["label"] in part
                assert beat["summary"] in part
                if beat["source"]:
                    assert f"via {beat['source']['name']}" in part or (
                        f'via <a href="{beat["source"]["url"]}"' in part
                    )
                    if beat["source"]["url"]:
                        assert beat["source"]["url"] in part


def test_the_text_part_spells_the_header_and_beat_lines_exactly():
    text = _digest(entries=ENTRIES).text

    assert "Heat 2 (USA, Dir: Michael Mann, 2026)\nWide release · 14 August 2026\n" in text
    assert (
        "Following: Michael Mann <https://app.example.com/person/1-michael-mann>, "
        "Legendary Pictures <https://app.example.com/studio/2-legendary>\n"
    ) in text
    assert "22 Sep · Casting [unconfirmed] · Ada is in talks.\n" in text
    assert "  via Deadline — https://deadline.example/heat-2-ada\n" in text
    assert "23 Sep · Production started · Cameras are rolling.\n  via TMDB\n" in text


def test_the_text_part_carries_links_as_bare_urls():
    envelope = _digest(slate=SLATE, entries=ENTRIES, overflow=2)

    assert "<a " not in envelope.text
    assert SLATE[0]["entries"][0]["film_url"] in envelope.text
    assert TIMELINE_URL in envelope.text


def test_no_clock_time_appears_in_either_part():
    envelope = _digest(slate=SLATE, entries=ENTRIES)

    assert not re.search(r"\d:\d\d", envelope.text)
    assert not re.search(r"\d:\d\d", envelope.html)


def test_only_a_rumored_beat_is_marked_unconfirmed():
    envelope = _digest(entries=ENTRIES)

    assert envelope.text.count("[unconfirmed]") == 1
    assert envelope.html.count("Unconfirmed") == 1
    html_casting = envelope.html[envelope.html.index("22 Sep") : envelope.html.index("23 Sep")]
    assert "Unconfirmed" in html_casting


def test_a_story_source_is_linked_and_tmdb_is_not():
    html = _digest(entries=[HEAT]).html

    assert '<a href="https://deadline.example/heat-2-ada"' in html
    assert "via TMDB" in html
    assert 'href="None"' not in html


def test_a_beat_with_no_source_has_no_source_line():
    text = _digest(entries=[ZODIAC]).text

    assert "21 Sep 2025 · New trailer · A trailer landed.\nAvailability from JustWatch" in text


def test_the_following_line_is_absent_when_the_entry_has_no_entity_follow():
    envelope = _digest(entries=[ZODIAC])

    for part in (envelope.text, envelope.html):
        assert "Following:" not in part


def test_the_justwatch_credit_appears_once_per_entry_that_needs_it_in_both_parts():
    only_heat = _digest(entries=[HEAT])
    both = _digest(entries=[ZODIAC, {**ZODIAC, "title": "Seven", "film_url": "https://x.test/7"}])

    for part in (only_heat.text, only_heat.html):
        assert "Availability from JustWatch" not in part
    for part in (both.text, both.html):
        assert part.count("Availability from JustWatch") == 2


def test_the_justwatch_credit_follows_the_entrys_last_beat():
    text = _digest(entries=[ZODIAC]).text

    assert text.index("A trailer landed.") < text.index("Availability from JustWatch")
    assert text.index("Availability from JustWatch") < text.index(ZODIAC["film_url"])


def test_the_cap_line_links_the_timeline_only_when_entries_were_left_off():
    capped = _digest(entries=ENTRIES, overflow=1)
    capped_more = _digest(entries=ENTRIES, overflow=3)
    uncapped = _digest(entries=ENTRIES)

    assert "and 1 more film on your timeline\nhttps://app.example.com/\n" in capped.text
    assert f'<a href="{TIMELINE_URL}"' in capped.html
    assert "and 1 more film on your timeline" in capped.html
    assert "and 3 more films on your timeline" in capped_more.text
    for part in (uncapped.text, uncapped.html):
        assert "more film" not in part
        assert "on your timeline</a>" not in part


def test_the_cap_line_closes_the_entries():
    text = _digest(entries=ENTRIES, overflow=1).text

    assert text.index(ZODIAC["film_url"]) < text.index("and 1 more film")
    assert text.index("and 1 more film") < text.index(SETTINGS_URL)


def test_the_preheader_is_a_hidden_first_element_in_html_only():
    envelope = _digest(entries=ENTRIES, preheader="Also: Zodiac — now streaming")

    assert "display:none" in envelope.html
    assert envelope.html.index("Also: Zodiac") < envelope.html.index("Hi Ada")
    assert "Also: Zodiac" not in envelope.text


def test_an_empty_preheader_renders_no_element():
    html = _digest(entries=ENTRIES, preheader="").html

    assert "display:none" not in html


def test_entries_render_in_the_order_given():
    envelope = _digest(entries=[ZODIAC, HEAT])

    for part in (envelope.text, envelope.html):
        assert part.index("Zodiac") < part.index("Heat 2")


def test_the_beats_render_under_their_films_header():
    envelope = _digest(entries=ENTRIES)

    for part in (envelope.text, envelope.html):
        assert part.index("Heat 2") < part.index("Ada is in talks.")
        assert part.index("Ada is in talks.") < part.index("Cameras are rolling.")
        assert part.index("Cameras are rolling.") < part.index("Zodiac")


def test_the_slate_lists_every_date_film_and_release_kind_in_both_parts():
    envelope = _digest(slate=SLATE, subject="Your slate: 1 upcoming date")

    for part in (envelope.text, envelope.html):
        assert "30 days" in part
        for day in SLATE:
            assert day["heading"] in part
            for item in day["entries"]:
                assert item["title"] in part
                assert item["release_label"] in part
                assert item["film_url"] in part
    assert "New on your timeline" not in envelope.html


def test_a_slate_marker_is_bracketed_in_text_and_a_pill_in_html():
    """DC-9: `[new]` / `[moved]` after the release kind in text, a pill in HTML, and nothing
    at all on a row whose date did not change."""
    envelope = _digest(slate=MARKED_SLATE, subject="Your slate: 3 upcoming dates")

    lines = envelope.text.splitlines()
    assert "  Arrival — Wide release [new]" in lines
    assert "  Blade — Wide release [moved]" in lines
    assert "  Casino — Wide release" in lines
    html = envelope.html
    arrival = html[html.index(">Arrival<") : html.index(">Blade<")]
    blade = html[html.index(">Blade<") : html.index(">Casino<")]
    casino = html[html.index(">Casino<") :]
    assert ">New</span>" in arrival and ">Moved</span>" not in arrival
    assert ">Moved</span>" in blade and ">New</span>" not in blade
    assert ">New</span>" not in casino and ">Moved</span>" not in casino


def test_a_slate_marker_pill_is_shaped_like_the_unconfirmed_pill():
    """One pill shape in the mail (NEU-1461): only the colours tell the two apart."""
    html = _digest(slate=MARKED_SLATE, entries=[HEAT]).html

    def pill(word: str) -> str:
        end = html.index(f">{word}</span>")
        return html[html.rindex("<span", 0, end) : end]

    shape = "padding:2px 6px;border-radius:4px;"
    for word in ("New", "Moved", "Unconfirmed"):
        assert shape in pill(word)
        assert "text-transform:uppercase" in pill(word)
    assert "background:#dcfce7" in pill("New")
    assert "background:#e0e7ff" in pill("Moved")


def test_the_slate_comes_before_the_timeline():
    """D-33: the weekly send *is* the slate mail, so the slate leads."""
    envelope = _digest(slate=SLATE, entries=ENTRIES)

    for part in (envelope.text.lower(), envelope.html.lower()):
        assert part.index("your slate") < part.index("on your timeline")


def test_both_parts_open_with_the_wordmark():
    """DC-14: one line of text naming the product heads the card — the same line the alert
    opens with, so the two mails read as one sender."""
    envelope = _digest(entries=ENTRIES, preheader="Also: Zodiac — now streaming")

    assert envelope.text.startswith("Backlotter\n\nHi Ada,")
    assert envelope.html.index("Also: Zodiac") < envelope.html.index(">Backlotter</p>")
    assert envelope.html.index(">Backlotter</p>") < envelope.html.index("Hi Ada")


def test_the_lead_card_renders_for_the_first_entry_only():
    """DC-14: the lead film is the lead card — 92px poster; every other entry is a compact
    row with the 62px one."""
    lead = {**ZODIAC, "poster_url": "https://image.tmdb.org/t/p/w185/zodiac.jpg"}
    row = {**HEAT, "poster_url": "https://image.tmdb.org/t/p/w154/heat.jpg"}
    third = {**ZODIAC, "title": "Seven", "film_url": "https://x.test/7"}

    html = _digest(entries=[lead, row, third]).html

    assert html.count('width="92"') == 2  # the lead's poster cell and its <img>
    assert f'<img src="{lead["poster_url"]}" width="92"' in html
    assert f'<img src="{row["poster_url"]}" width="62"' in html
    assert f'<img src="{third["poster_url"]}" width="62"' in html
    assert html.index(lead["poster_url"]) < html.index("Heat 2")


def test_every_image_is_sized_as_specified():
    """Only two poster widths exist: 92px on the lead card, 62px on compact rows and the
    slate — each `<img` carries the width as an attribute and in its style, because Outlook
    reads the one and every other client the other."""
    lead = {**ZODIAC, "poster_url": "https://image.tmdb.org/t/p/w185/zodiac.jpg"}
    row = {**HEAT, "poster_url": "https://image.tmdb.org/t/p/w154/heat.jpg"}

    html = _digest(slate=SLATE, entries=[lead, row]).html

    imgs = re.findall(r'<img [^>]*width="(\d+)"[^>]*width:(\d+)px', html)
    assert imgs == [("62", "62"), ("92", "92"), ("62", "62")]


def test_a_digest_of_one_entry_has_a_lead_card_and_no_rows():
    lead = {**ZODIAC, "poster_url": "https://image.tmdb.org/t/p/w185/zodiac.jpg"}

    html = _digest(entries=[lead]).html

    assert f'<img src="{lead["poster_url"]}" width="92"' in html
    assert 'width="62"' not in html


def test_the_unconfirmed_marker_is_the_feeds_amber_pill():
    html = _digest(entries=[ZODIAC, HEAT]).html

    pill = html[html.rindex("<span", 0, html.index("Unconfirmed")) : html.index("Unconfirmed")]
    assert "background:#fef3c7" in pill
    assert "color:#92400e" in pill
    assert "text-transform:uppercase" in pill


def test_the_poster_is_rendered_when_there_is_one_and_omitted_when_there_is_not():
    with_poster = _digest(entries=[ZODIAC]).html
    without = _digest(entries=[HEAT]).html

    assert "<img" in with_poster
    assert ZODIAC["poster_url"] in with_poster
    assert "<img" not in without


def test_both_parts_carry_the_settings_link_and_say_why_the_mail_arrived():
    envelope = _digest(entries=ENTRIES)

    for part in (envelope.text, envelope.html):
        assert SETTINGS_URL in part
        assert "films you follow" in part
        assert "weekly digest" in part


def test_the_footer_links_unsubscribe_to_the_token_url_and_keeps_the_settings_link():
    """DC-10: the footer's "unsubscribe" is the one-click link; the settings link stays for
    changing the cadence rather than stopping it."""
    envelope = _digest(entries=ENTRIES)

    assert f'<a href="{UNSUBSCRIBE_URL}" style="color:#1a56db;">unsubscribe</a>' in envelope.html
    assert f"Or unsubscribe in one click:\n\n{UNSUBSCRIBE_URL}\n" in envelope.text
    for part in (envelope.text, envelope.html):
        assert SETTINGS_URL in part


def test_without_a_token_the_footer_offers_the_settings_link_alone():
    """`render_digest` previews a user with no settings row without writing one, so there is
    no token to link — the footer falls back to the settings page's "or stop it"."""
    envelope = _digest(entries=ENTRIES, unsubscribe_url=None)

    for part in (envelope.text, envelope.html):
        assert "unsubscribe" not in part
        assert "or stop it" in part
        assert SETTINGS_URL in part


def test_display_name_is_optional_the_way_every_other_template_makes_it():
    envelope = _digest(entries=ENTRIES, display_name="")

    assert envelope.text.startswith("Backlotter\n\nHi,")


def test_markup_escapes_in_html_and_not_in_text():
    entry = {
        **HEAT,
        "title": "Heat & <Sons>",
        "beats": [{**HEAT["beats"][0], "summary": "A <b>bold</b> & brave move."}],
    }

    envelope = _digest(entries=[entry])

    assert "Heat &amp; &lt;Sons&gt;" in envelope.html
    assert "A &lt;b&gt;bold&lt;/b&gt; &amp; brave move." in envelope.html
    assert "Heat & <Sons>" in envelope.text
    assert "A <b>bold</b> & brave move." in envelope.text


def test_a_digest_with_nothing_to_say_is_not_a_mail():
    """Not reachable through `digest_sender.render_batch`, which renders nothing for an empty
    batch — asserted so a future caller that does not gets a loud failure rather than a
    subjectless mail."""
    with pytest.raises(MailError):
        _digest(subject="")
