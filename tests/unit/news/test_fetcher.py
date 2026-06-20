import httpx

from upmovies.news.fetcher import _looks_blocked


def _resp(status, content_type):
    return httpx.Response(status, headers={"Content-Type": content_type})


def test_blocked_on_429_and_403():
    assert _looks_blocked(_resp(429, "text/html")) is True
    assert _looks_blocked(_resp(403, "text/html")) is True


def test_blocked_on_200_html_interstitial():
    # A throttle/captcha page is HTML, not the RSS XML feed.
    assert _looks_blocked(_resp(200, "text/html; charset=utf-8")) is True


def test_not_blocked_on_200_xml():
    assert _looks_blocked(_resp(200, "application/xml")) is False
    assert _looks_blocked(_resp(200, "text/xml; charset=UTF-8")) is False
