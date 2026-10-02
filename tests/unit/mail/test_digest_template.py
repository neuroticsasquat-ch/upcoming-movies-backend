"""The `digest` template's copy (NEU-1381, NEU-1460, NEU-1461, NEU-1462, NEU-1528,
NEU-1529): what the daily and weekly digests say — the daily as the timeline day reproduced
(day → follow block → section → update type → film or entity row → line), the weekly by entry
(follow block → section → update type → film or entity entry → dated line) — beside the slate
and its new/moved markers, under a wordmark.

`test_templates.py` asserts the rendering rules against whichever template is handy; this
asserts the digest's own copy. The timeline is rendered through `digest_sender.render_batch`
from hand-built lines, so what is asserted is the mail a batch becomes; the slate and the
chrome around it are rendered from a hand-built context in the shape `digest_context` builds,
because the template computes nothing: every string it shows arrives here as a value."""

import re
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from upmovies.app.services.digest_sender import (
    DigestBatch,
    DigestBeat,
    DigestFilm,
    DigestLine,
    DigestReach,
    DigestRecipient,
    DigestSource,
    render_batch,
)
from upmovies.config import get_settings
from upmovies.mail import Envelope, MailError, render

SETTINGS_URL = "https://app.example.com/settings"
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


# --- the timeline, rendered from lines ---------------------------------------------------

TODAY = date(2026, 10, 2)
DAY = datetime(2026, 10, 2, 9, tzinfo=UTC)
YESTERDAY = DAY - timedelta(days=1)
BASE = "https://app.example.com"


def _film(title: str, n: int, *, poster: bool = True) -> DigestFilm:
    return DigestFilm(
        film_id=uuid4(),
        tmdb_id=n,
        title=title,
        film_url=f"{BASE}/film/{n}",
        poster_url=f"https://image.tmdb.org/t/p/w154/{n}.jpg" if poster else None,
        parenthetical="US, 2026",
    )


DUNE = _film("Dune: Part Three", 1)
HEAT = _film("Heat 2", 2)
CLEO = _film("Cleopatra", 3, poster=False)

VILLENEUVE = DigestReach("person", "5", "Denis Villeneuve", f"{BASE}/person/5-denis")
LEGENDARY = DigestReach("company", "9", "Legendary Pictures", f"{BASE}/studio/9-legendary")
UNNAMED_FRANCHISE = DigestReach("franchise", "7", None, None)


def _beat(
    film: DigestFilm,
    event_type: str,
    summary: str,
    *,
    news: bool = False,
    rumored: bool = False,
    at: datetime = DAY,
) -> DigestBeat:
    return DigestBeat(
        notification_id=uuid4(),
        event_id=uuid4(),
        event_type=event_type,
        created_at=at,
        occurred_at=at,
        confidence="rumored" if rumored else "confirmed",
        summary=summary,
        source=DigestSource("Variety", "https://variety.example/x") if news else None,
        news_backed=news,
        film=film,
    )


CANCEL = _beat(CLEO, "canceled", "The film has been canceled.")

ALL_BLOCKS = (
    DigestLine(_beat(DUNE, "casting", "Zendaya returns.", news=True, rumored=True), None),
    DigestLine(_beat(HEAT, "release_date", "US wide date slipped."), None),
    DigestLine(_beat(HEAT, "now_available", "Now streaming.", at=YESTERDAY), None),
    DigestLine(_beat(DUNE, "crew_attached", "Villeneuve will direct.", news=True), VILLENEUVE),
    DigestLine(CANCEL, VILLENEUVE),
    DigestLine(CANCEL, LEGENDARY),
    DigestLine(_beat(DUNE, "company_attached", "Legendary joins."), LEGENDARY),
    DigestLine(_beat(HEAT, "collection_attached", "Filed under a franchise."), UNNAMED_FRANCHISE),
)
"""Two days; all four blocks; both sections; an entity in both sections; a card two entities
reached; an entity the catalog cannot name; a `now_available` under Not yet reported."""


def _mail(*lines: DigestLine, cadence: str = "daily") -> Envelope:
    batch = DigestBatch(
        recipient=DigestRecipient(
            user_id=uuid4(),
            email="ada@example.com",
            display_name="Ada",
            deliverable=True,
            unsubscribe_token="tok-ada",
        ),
        lines=lines,
        unsendable=(),
    )
    envelope = render_batch(
        batch,
        cadence=cadence,  # type: ignore[arg-type]
        today=TODAY,
        settings=get_settings().model_copy(update={"product_name": "Backlotter"}),
    )
    assert envelope is not None
    return envelope


DAILY_TIMELINE = f"""NEW ON YOUR TIMELINE

Friday, October 2, 2026

FILMS

In the news

Dune: Part Three (US, 2026) — {BASE}/film/1
  Casting [unconfirmed] · Zendaya returns.
    via Variety — https://variety.example/x

Not yet reported (unconfirmed)

-- Release date --

Heat 2 (US, 2026) — {BASE}/film/2
  US wide date slipped.

PEOPLE

In the news

Denis Villeneuve — {BASE}/person/5-denis
  Crew attached · Dune: Part Three (US, 2026) · Villeneuve will direct. — {BASE}/film/1
    via Variety — https://variety.example/x

Not yet reported (unconfirmed)

-- Canceled --

Denis Villeneuve — {BASE}/person/5-denis
  Cleopatra (US, 2026) · The film has been canceled. — {BASE}/film/3

STUDIOS

Not yet reported (unconfirmed)

-- Attached --

Legendary Pictures — {BASE}/studio/9-legendary
  Dune: Part Three (US, 2026) · Legendary joins. — {BASE}/film/1

-- Canceled --

Legendary Pictures — {BASE}/studio/9-legendary
  Cleopatra (US, 2026) · The film has been canceled. — {BASE}/film/3

FRANCHISES

Not yet reported (unconfirmed)

-- Attached --

A franchise you follow
  Heat 2 (US, 2026) · Filed under a franchise. — {BASE}/film/2

Thursday, October 1, 2026

FILMS

Not yet reported (unconfirmed)

-- Now available --

Heat 2 (US, 2026) — {BASE}/film/2
  Now streaming.

Availability from JustWatch

You are getting this daily digest"""


def test_the_daily_text_part_is_the_timeline_day_by_day():
    """FB-18, FB-20, FB-24: day → block → section → update type → row → line, newest day
    first; a film row headed by the film and its link, an entity row by the entity and its
    link, each entity line naming and linking its film; beat labels only under In the news and
    Other updates, the Unconfirmed marker only under In the news, no "via TMDB"."""
    text = _mail(*ALL_BLOCKS).text

    assert DAILY_TIMELINE in text


def test_the_daily_html_part_carries_the_same_tree_under_a_heading_ladder():
    html = _mail(*ALL_BLOCKS).html

    order = [
        ">New on your timeline</h2>",
        ">Friday, October 2, 2026</h3>",
        ">Films</h4>",
        ">In the news</h5>",
        "Zendaya returns.",
        ">Not yet reported <span",
        ">Release date</h6>",
        "US wide date slipped.",
        ">People</h4>",
        "Villeneuve will direct.",
        ">Canceled</h6>",
        ">Studios</h4>",
        ">Attached</h6>",
        "Legendary joins.",
        ">Franchises</h4>",
        "A franchise you follow",
        ">Thursday, October 1, 2026</h3>",
        ">Now available</h6>",
        "Now streaming.",
        "Availability from JustWatch",
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert f'<a href="{BASE}/person/5-denis"' in html
    assert f'<a href="{BASE}/studio/9-legendary"' in html
    assert html.count("The film has been canceled.") == 2


def test_an_entity_the_catalog_cannot_name_is_its_fallback_unlinked():
    html = _mail(*ALL_BLOCKS).html

    fallback = html.index("A franchise you follow")
    assert html.rindex("<p", 0, fallback) > html.rindex("<a ", 0, fallback)


def test_the_retired_furniture_is_gone_from_both_parts():
    """FB-21: no "Following:" line, no overflow line, no "See the film page", no lead card."""
    envelope = _mail(*ALL_BLOCKS)

    for part in (envelope.text, envelope.html):
        assert "Following:" not in part
        assert "more film" not in part
        assert "on your timeline</a>" not in part
        assert "See the film page" not in part
        assert "via TMDB" not in part
    assert 'width="92"' not in envelope.html


def test_each_day_has_one_poster_strip_of_its_films_de_duplicated():
    """FB-7: Dune is reached three ways on the first day and is one poster; Cleopatra has no
    poster and is left out; the second day's strip is Heat 2 alone."""
    html = _mail(*ALL_BLOCKS).html

    imgs = re.findall(r'<img src="([^"]+)" width="(\d+)"[^>]*width:(\d+)px', html)
    assert imgs == [
        ("https://image.tmdb.org/t/p/w154/1.jpg", "52", "52"),
        ("https://image.tmdb.org/t/p/w154/2.jpg", "52", "52"),
        ("https://image.tmdb.org/t/p/w154/2.jpg", "52", "52"),
    ]
    assert html.index(">Friday, October 2, 2026</h3>") < html.index("w154/1.jpg")


def test_the_poster_strip_is_capped_at_eight():
    lines = [DigestLine(_beat(_film(f"Film {n}", 10 + n), "casting", "x"), None) for n in range(12)]

    assert _mail(*lines).html.count("<img ") == 8


def test_only_an_in_the_news_rumored_beat_is_marked_unconfirmed():
    rumored_catalog = DigestLine(_beat(HEAT, "casting", "In talks.", rumored=True), None)
    envelope = _mail(*ALL_BLOCKS, rumored_catalog)

    assert envelope.text.count("[unconfirmed]") == 1
    assert envelope.html.count(">Unconfirmed</span>") == 1
    pill_end = envelope.html.index(">Unconfirmed</span>")
    pill = envelope.html[envelope.html.rindex("<span", 0, pill_end) : pill_end]
    assert "background:#fef3c7" in pill and "color:#92400e" in pill


def test_justwatch_is_credited_once_under_now_available_and_per_news_film_row():
    """NR-8 under Not yet reported (once for the heading, however many films); DC-17 under In
    the news (once per film row with a `now_available` line)."""
    catalog = [
        DigestLine(_beat(HEAT, "now_available", "Now streaming."), None),
        DigestLine(_beat(DUNE, "now_available", "Now renting."), None),
    ]
    news = [DigestLine(_beat(CLEO, "now_available", "On Netflix.", news=True), None)]

    for lines, count in ((catalog, 1), (news, 1), (catalog + news, 2)):
        envelope = _mail(*lines)
        for part in (envelope.text, envelope.html):
            assert part.count("Availability from JustWatch") == count
    text = _mail(*catalog).text
    assert text.index("Now renting.") < text.index("Now streaming.")
    assert text.index("Now streaming.") < text.index("Availability from JustWatch")


WEEK = (
    *ALL_BLOCKS,
    DigestLine(_beat(HEAT, "crew_attached", "Villeneuve will produce.", at=YESTERDAY), VILLENEUVE),
    DigestLine(_beat(DUNE, "casting", "Villeneuve cameos."), VILLENEUVE),
)
"""`ALL_BLOCKS` plus an entity entry that spans two films on two days, published in the
reverse of their films' title order."""

WEEKLY_TIMELINE = f"""NEW ON YOUR TIMELINE

FILMS

In the news

Dune: Part Three (US, 2026) — {BASE}/film/1
  2 Oct · Casting [unconfirmed] · Zendaya returns.
    via Variety — https://variety.example/x

Not yet reported (unconfirmed)

-- Now available --

Heat 2 (US, 2026) — {BASE}/film/2
  1 Oct · Now streaming.

Availability from JustWatch

-- Release date --

Heat 2 (US, 2026) — {BASE}/film/2
  2 Oct · US wide date slipped.

PEOPLE

In the news

Denis Villeneuve — {BASE}/person/5-denis
  2 Oct · Crew attached · Dune: Part Three (US, 2026) · Villeneuve will direct. — {BASE}/film/1
    via Variety — https://variety.example/x

Not yet reported (unconfirmed)

-- Attached --

Denis Villeneuve — {BASE}/person/5-denis
  1 Oct · Heat 2 (US, 2026) · Villeneuve will produce. — {BASE}/film/2
  2 Oct · Dune: Part Three (US, 2026) · Villeneuve cameos. — {BASE}/film/1

-- Canceled --

Denis Villeneuve — {BASE}/person/5-denis
  2 Oct · Cleopatra (US, 2026) · The film has been canceled. — {BASE}/film/3

STUDIOS

Not yet reported (unconfirmed)

-- Attached --

Legendary Pictures — {BASE}/studio/9-legendary
  2 Oct · Dune: Part Three (US, 2026) · Legendary joins. — {BASE}/film/1

-- Canceled --

Legendary Pictures — {BASE}/studio/9-legendary
  2 Oct · Cleopatra (US, 2026) · The film has been canceled. — {BASE}/film/3

FRANCHISES

Not yet reported (unconfirmed)

-- Attached --

A franchise you follow
  2 Oct · Heat 2 (US, 2026) · Filed under a franchise. — {BASE}/film/2

You are getting this weekly digest"""


def test_the_weekly_text_part_reads_by_entry_with_every_line_dated():
    """FB-19, FB-24: no day headings; one entry per film or entity under each section and
    update type it touched, across both days — Heat 2 under Now available and Release date,
    Villeneuve under In the news, Attached and Canceled — every line dated, an entity entry's
    lines in publication order rather than by film."""
    assert WEEKLY_TIMELINE in _mail(*WEEK, cadence="weekly").text


def test_the_weekly_html_part_has_no_day_headings_and_dates_every_line():
    html = _mail(*WEEK, cadence="weekly").html

    assert "<h3" not in html
    for part in ("October 2, 2026", "October 1, 2026"):
        assert part not in html
    order = [
        ">New on your timeline</h2>",
        ">Films</h4>",
        ">In the news</h5>",
        "<strong>2 Oct · Casting</strong>",
        ">Now available</h6>",
        "<strong>1 Oct</strong>",
        "Now streaming.",
        ">Release date</h6>",
        ">People</h4>",
        ">Attached</h6>",
        "Villeneuve will produce.",
        "Villeneuve cameos.",
        ">Studios</h4>",
        ">Franchises</h4>",
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert html.count("<strong>2 Oct") + html.count("<strong>1 Oct") == len(WEEK)


def test_the_weekly_has_one_poster_strip_at_the_top_of_the_timeline():
    """FB-19: the week's films, de-duplicated, in one strip under "New on your timeline" —
    not one per day, though the week spans two."""
    html = _mail(*WEEK, cadence="weekly").html

    assert html.count('style="margin:0 0 12px;"') == 1  # the strip's table
    imgs = re.findall(r'<img src="([^"]+)" width="52"', html)
    assert imgs == [
        "https://image.tmdb.org/t/p/w154/1.jpg",
        "https://image.tmdb.org/t/p/w154/2.jpg",
    ]
    assert html.index(">New on your timeline</h2>") < html.index("w154/1.jpg")
    assert html.index("w154/2.jpg") < html.index(">Films</h4>")


def test_the_text_part_carries_links_as_bare_urls():
    envelope = _mail(*ALL_BLOCKS)

    assert "<a " not in envelope.text
    assert f"{BASE}/film/1" in envelope.text


def test_no_clock_time_appears_in_either_part():
    envelope = _mail(*ALL_BLOCKS)

    assert not re.search(r"\d:\d\d", envelope.text)
    assert not re.search(r"\d:\d\d", envelope.html)


def test_markup_escapes_in_html_and_not_in_text():
    film = _film("Heat & <Sons>", 50)
    line = DigestLine(_beat(film, "casting", "A <b>bold</b> & brave move.", news=True), None)

    envelope = _mail(line)

    assert "Heat &amp; &lt;Sons&gt;" in envelope.html
    assert "A &lt;b&gt;bold&lt;/b&gt; &amp; brave move." in envelope.html
    assert "Heat & <Sons>" in envelope.text
    assert "A <b>bold</b> & brave move." in envelope.text


# --- the slate and the chrome, from a hand-built context ----------------------------------


def _digest(
    *,
    slate=(),
    days=(),
    cadence="weekly",
    subject="Heat 2 — casting, + 1 more film",
    preheader="",
    **overrides,
):
    context: dict[str, object] = {
        "product_name": "Backlotter",
        "display_name": "Ada",
        "settings_url": SETTINGS_URL,
        "cadence": cadence,
        "slate_window_days": 30,
        "subject": subject,
        "preheader": preheader,
        "slate": list(slate),
        "days": list(days),
        "week": None,
        "unsubscribe_url": UNSUBSCRIBE_URL,
        **overrides,
    }
    return render(
        "digest", context, sender="Backlotter <no-reply@example.com>", to="ada@example.com"
    )


ONE_DAY = [
    {
        "heading": "Friday, October 2, 2026",
        "posters": [],
        "blocks": [
            {
                "label": "Films",
                "sections": [
                    {
                        "label": "In the news",
                        "qualifier": None,
                        "rows": [
                            {
                                "film": {
                                    "title": "Heat 2",
                                    "parenthetical": "US, 2026",
                                    "url": f"{BASE}/film/2",
                                },
                                "entity": None,
                                "lines": [
                                    {
                                        "prefix": "Casting",
                                        "unconfirmed": True,
                                        "film": None,
                                        "summary": "Ada is in talks.",
                                        "source": None,
                                    }
                                ],
                                "credits_justwatch": False,
                            }
                        ],
                        "update_types": [],
                    }
                ],
            }
        ],
    }
]


def test_the_subject_is_the_contexts_verbatim():
    envelope = _digest(days=ONE_DAY, subject="Heat 2 — casting, + 1 more film · your slate")

    assert envelope.subject == "Heat 2 — casting, + 1 more film · your slate"


def test_the_preheader_is_a_hidden_first_element_in_html_only():
    envelope = _digest(days=ONE_DAY, preheader="Also: Zodiac — now available")

    assert "display:none" in envelope.html
    assert envelope.html.index("Also: Zodiac") < envelope.html.index("Hi Ada")
    assert "Also: Zodiac" not in envelope.text


def test_an_empty_preheader_renders_no_element():
    html = _digest(days=ONE_DAY, preheader="").html

    assert "display:none" not in html


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
    html = _digest(slate=MARKED_SLATE, days=ONE_DAY).html

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
    envelope = _digest(slate=SLATE, days=ONE_DAY)

    for part in (envelope.text.lower(), envelope.html.lower()):
        assert part.index("your slate") < part.index("on your timeline")


def test_both_parts_open_with_the_wordmark():
    """DC-14: one line of text naming the product heads the card."""
    envelope = _digest(days=ONE_DAY, preheader="Also: Zodiac — now available")

    assert envelope.text.startswith("Backlotter\n\nHi Ada,")
    assert envelope.html.index("Also: Zodiac") < envelope.html.index(">Backlotter</p>")
    assert envelope.html.index(">Backlotter</p>") < envelope.html.index("Hi Ada")


def test_the_slate_rows_keep_their_62px_posters():
    html = _digest(slate=SLATE).html

    imgs = re.findall(r'<img [^>]*width="(\d+)"[^>]*width:(\d+)px', html)
    assert imgs == [("62", "62")]


def test_both_parts_carry_the_settings_link_and_say_why_the_mail_arrived():
    envelope = _digest(days=ONE_DAY)

    for part in (envelope.text, envelope.html):
        assert SETTINGS_URL in part
        assert "films you follow" in part
        assert "weekly digest" in part


def test_the_footer_links_unsubscribe_to_the_token_url_and_keeps_the_settings_link():
    """DC-10: the footer's "unsubscribe" is the one-click link; the settings link stays for
    changing the cadence rather than stopping it."""
    envelope = _digest(days=ONE_DAY)

    assert f'<a href="{UNSUBSCRIBE_URL}" style="color:#1a56db;">unsubscribe</a>' in envelope.html
    assert f"Or unsubscribe in one click:\n\n{UNSUBSCRIBE_URL}\n" in envelope.text
    for part in (envelope.text, envelope.html):
        assert SETTINGS_URL in part


def test_without_a_token_the_footer_offers_the_settings_link_alone():
    """`render_digest` previews a user with no settings row without writing one, so there is
    no token to link — the footer falls back to the settings page's "or stop it"."""
    envelope = _digest(days=ONE_DAY, unsubscribe_url=None)

    for part in (envelope.text, envelope.html):
        assert "unsubscribe" not in part
        assert "or stop it" in part
        assert SETTINGS_URL in part


def test_display_name_is_optional_the_way_every_other_template_makes_it():
    envelope = _digest(days=ONE_DAY, display_name="")

    assert envelope.text.startswith("Backlotter\n\nHi,")


def test_a_digest_with_nothing_to_say_is_not_a_mail():
    """Not reachable through `digest_sender.render_batch`, which renders nothing for an empty
    batch — asserted so a future caller that does not gets a loud failure rather than a
    subjectless mail."""
    with pytest.raises(MailError):
        _digest(subject="")
