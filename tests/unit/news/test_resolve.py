from pathlib import Path

import httpx
import respx

from upmovies.news.resolve import is_google_news_url, resolve_google_news_url

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "google_news"
ARTICLE_PAGE = (FIXTURES / "article_page.html").read_text()
BATCH_RESPONSE = (FIXTURES / "batchexecute_response.txt").read_text()
GN_URL = "https://news.google.com/rss/articles/CBMiSPIKEID"
BATCH_ENDPOINT = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def test_is_google_news_url_true():
    assert is_google_news_url("https://news.google.com/rss/articles/CBMiabc") is True


def test_is_google_news_url_false():
    assert is_google_news_url("https://variety.com/2026/film/news/abc") is False
    assert is_google_news_url("https://news.google.com/topics/xyz") is False


def test_is_google_news_url_rejects_spoofed_host():
    assert is_google_news_url("https://evil.internal/news.google.com/articles/x") is False
    assert is_google_news_url("https://news.google.com.evil.test/articles/x") is False


@respx.mock
async def test_resolve_happy_path():
    respx.get(GN_URL).mock(return_value=httpx.Response(200, text=ARTICLE_PAGE))
    respx.post(BATCH_ENDPOINT).mock(return_value=httpx.Response(200, text=BATCH_RESPONSE))
    async with httpx.AsyncClient() as client:
        result = await resolve_google_news_url(client, GN_URL)
    assert result is not None
    assert result.startswith("http")
    assert "news.google.com" not in result


async def test_resolve_non_google_url_returns_none():
    async with httpx.AsyncClient() as client:
        assert await resolve_google_news_url(client, "https://variety.com/x") is None


@respx.mock
async def test_resolve_missing_signature_returns_none():
    respx.get(GN_URL).mock(return_value=httpx.Response(200, text="<html>no attrs</html>"))
    async with httpx.AsyncClient() as client:
        assert await resolve_google_news_url(client, GN_URL) is None


@respx.mock
async def test_resolve_429_returns_none():
    respx.get(GN_URL).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        assert await resolve_google_news_url(client, GN_URL) is None


@respx.mock
async def test_resolve_malformed_batch_no_separator_returns_none():
    # Body has no \n\n separator → len(parts) < 2 guard fires
    respx.get(GN_URL).mock(return_value=httpx.Response(200, text=ARTICLE_PAGE))
    respx.post(BATCH_ENDPOINT).mock(return_value=httpx.Response(200, text="garbage not json"))
    async with httpx.AsyncClient() as client:
        assert await resolve_google_news_url(client, GN_URL) is None


@respx.mock
async def test_resolve_malformed_batch_json_decode_error_returns_none():
    # Body has the XSSI prefix + blank line but invalid JSON → json.JSONDecodeError branch fires
    respx.get(GN_URL).mock(return_value=httpx.Response(200, text=ARTICLE_PAGE))
    respx.post(BATCH_ENDPOINT).mock(return_value=httpx.Response(200, text=")]}'\n\nnot valid json"))
    async with httpx.AsyncClient() as client:
        assert await resolve_google_news_url(client, GN_URL) is None


@respx.mock
async def test_resolve_503_batch_returns_none():
    # Non-200 response from batchexecute POST → None
    respx.get(GN_URL).mock(return_value=httpx.Response(200, text=ARTICLE_PAGE))
    respx.post(BATCH_ENDPOINT).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        assert await resolve_google_news_url(client, GN_URL) is None
