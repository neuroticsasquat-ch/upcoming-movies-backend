import httpx

from upmovies.news.fetcher import _looks_blocked, parse_feed


def _resp(status, content_type):
    return httpx.Response(status, headers={"Content-Type": content_type})


def _google_item(title: str, url: str, *, outlet: str | None) -> str:
    source_el = f'<source url="https://example.com">{outlet}</source>' if outlet else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Google News</title>'
        f"<item><title>{title}</title><link>{url}</link>{source_el}</item>"
        "</channel></rss>"
    )


def test_blocked_on_429_and_403():
    assert _looks_blocked(_resp(429, "text/html")) is True
    assert _looks_blocked(_resp(403, "text/html")) is True


def test_blocked_on_200_html_interstitial():
    # A throttle/captcha page is HTML, not the RSS XML feed.
    assert _looks_blocked(_resp(200, "text/html; charset=utf-8")) is True


def test_not_blocked_on_200_xml():
    assert _looks_blocked(_resp(200, "application/xml")) is False
    assert _looks_blocked(_resp(200, "text/xml; charset=UTF-8")) is False


def test_parse_feed_resolves_outlet_from_source_element():
    feed = _google_item(
        "Spidey Lands Director - Variety", "https://news.example/a", outlet="Deadline"
    )
    entries = parse_feed("Google News: per-film", feed)
    assert entries[0].outlet == "Deadline"  # <source> wins over the title suffix


def test_parse_feed_resolves_outlet_from_title_when_no_source_element():
    feed = _google_item("Spidey Lands Director - Variety", "https://news.example/b", outlet=None)
    entries = parse_feed("Google News: per-film", feed)
    assert entries[0].outlet == "Variety"


def test_parse_feed_leaves_outlet_none_for_trade_feeds():
    feed = _google_item("Some Headline - Deadline", "https://news.example/c", outlet="Deadline")
    entries = parse_feed("Deadline", feed)
    assert entries[0].outlet is None  # trade feeds keep their clean source label
