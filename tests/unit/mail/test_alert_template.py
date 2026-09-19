"""The `alert` template's copy (NEU-1380): what a watchlist alert actually says, and the two
shapes it has to render — one film, and several in one mail.

Beside `test_templates.py` rather than inside it because that file asserts the *rendering
rules* (the three strictnesses) against whichever template is handy; this one asserts the
alert's own copy, which is a product decision and changes for different reasons."""

import pytest

from upmovies.mail import MailError, render

DUNE = {
    "title": "Dune: Part Three",
    "beat": "Release date",
    "summary": "US wide release date slipped from 1 May 2026 to 18 December 2026.",
    "film_url": "https://app.example.com/film/1234-dune-part-three",
    "poster_url": "https://image.tmdb.org/t/p/w154/dune.jpg",
}
HEAT = {
    "title": "Heat 2",
    "beat": "Now available",
    "summary": "Now streaming on Max.",
    "film_url": "https://app.example.com/film/5678-heat-2",
    "poster_url": None,
}
SETTINGS_URL = "https://app.example.com/settings"


def _alert(items, **overrides):
    context: dict[str, object] = {
        "product_name": "Backlotter",
        "display_name": "Ada",
        "settings_url": SETTINGS_URL,
        "items": items,
        **overrides,
    }
    return render(
        "alert", context, sender="Backlotter <no-reply@example.com>", to="ada@example.com"
    )


def test_one_alert_names_the_film_and_the_beat_in_the_subject():
    """A subject a reader can act on from the notification shade, which is where most of these
    are actually read."""
    envelope = _alert([DUNE])

    assert envelope.subject == "Dune: Part Three — release date"


def test_several_alerts_in_one_mail_count_themselves_in_the_subject():
    """The batch is the point (D-31): three films that moved on one day are one mail, and
    naming one of them in the subject would misrepresent the other two."""
    envelope = _alert([DUNE, HEAT])

    assert envelope.subject == "2 updates from your watchlist"


def test_every_item_carries_its_title_summary_and_film_link_in_both_parts():
    envelope = _alert([DUNE, HEAT])

    for part in (envelope.text, envelope.html):
        for item in (DUNE, HEAT):
            assert item["title"] in part
            assert item["summary"] in part
            assert item["film_url"] in part
            assert item["beat"] in part


def test_the_text_part_carries_the_film_link_as_a_bare_url():
    """The plain-text part has to stand alone (`types.Envelope`) — an `<a>` is not a link a
    text client can follow, and the film page is the whole call to action."""
    envelope = _alert([DUNE])

    assert "<a " not in envelope.text
    assert DUNE["film_url"] in envelope.text


def test_the_poster_is_rendered_when_there_is_one_and_omitted_when_there_is_not():
    """A film with no poster drops the cell rather than rendering an empty `<img>` — see
    `alert_sender.poster_url`."""
    with_poster = _alert([DUNE]).html
    without = _alert([HEAT]).html

    assert DUNE["poster_url"] in with_poster
    assert "<img" in with_poster
    assert "<img" not in without


def test_both_parts_carry_the_settings_link_and_say_why_the_mail_arrived():
    """The unsubscribe half of the spec's template contract. The 'why' line matters as much as
    the link: a mail that cannot explain itself reads as spam whatever it links to."""
    envelope = _alert([DUNE])

    assert SETTINGS_URL in envelope.text
    assert SETTINGS_URL in envelope.html
    assert "watchlist" in envelope.text
    assert "watchlist" in envelope.html


def test_display_name_is_optional_the_way_every_other_template_makes_it():
    envelope = _alert([DUNE], display_name="")

    assert envelope.text.startswith("Hi,")


def test_a_title_with_markup_in_it_escapes_in_html_and_not_in_text():
    """Film titles are TMDB data, and the HTML part is the one that renders them as markup."""
    item = {**DUNE, "title": "Dune & <Sons>"}

    envelope = _alert([item])

    assert "Dune &amp; &lt;Sons&gt;" in envelope.html
    assert "Dune & <Sons>" in envelope.text


def test_an_alert_with_no_items_is_a_mail_with_nothing_to_say():
    """Not reachable through `alert_sender.send_batch`, which returns early on an empty batch —
    asserted so that a future caller which does not gets a loud failure rather than a mail
    saying '0 updates from your watchlist'."""
    with pytest.raises(MailError):
        _alert([])
