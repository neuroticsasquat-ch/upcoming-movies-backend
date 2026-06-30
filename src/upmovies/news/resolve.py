"""Resolve a Google News RSS article URL to its real publisher URL via Google's
batchexecute endpoint. Redirect-following does not work (Google encodes these
since ~2024); this scrapes the per-article signature+timestamp from the article
page and posts them to the internal RPC. Best-effort: every failure path returns
None — the caller falls back to the original Google URL."""

import json
import logging
import re
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

_BATCH_ENDPOINT = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_SIG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')


def is_google_news_url(url: str) -> bool:
    """True only for the encoded RSS article redirects we can decode.

    NOTE: This is the SSRF boundary for the one outbound fetch in
    resolve_google_news_url. The hostname check is intentional — do NOT
    loosen it to a substring match (that allows spoofed paths like
    evil.internal/news.google.com/articles/x to bypass the guard).
    """
    try:
        parsed = urlsplit(url)
        return parsed.hostname == "news.google.com" and "/articles/" in parsed.path
    except Exception:
        return False


def _article_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1].split("?")[0]


def _build_freq(article_id: str, timestamp: str, signature: str) -> str:
    inner = json.dumps(
        [
            "garturlreq",
            [
                [
                    "X",
                    "X",
                    ["X", "X"],
                    None,
                    None,
                    1,
                    1,
                    "US:en",
                    None,
                    1,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    1,
                ],
                "X",
                "X",
                1,
                [1, 1, 1],
                1,
                1,
                None,
                0,
                0,
                None,
                0,
            ],
            article_id,
            int(timestamp),
            signature,
        ]
    )
    return json.dumps([[["Fbv4je", inner]]])


def _extract_publisher_url(body: str) -> str | None:
    """Parse the batchexecute response for the publisher URL. CONFIRMED against live
    Google by the Task 1 spike. Google prefixes the body with the XSSI guard `)]}'`
    then a blank line, then one JSON document. The `Fbv4je` row's third element is a
    JSON string (`["garturlres", "<publisher url>", 1]`); the URL is its index [1]."""
    parts = body.split("\n\n", 1)
    if len(parts) < 2:
        return None
    try:
        outer = json.loads(parts[1])
    except json.JSONDecodeError:
        return None
    for entry in outer:
        if isinstance(entry, list) and len(entry) > 2 and entry[1] == "Fbv4je" and entry[2]:
            inner = json.loads(entry[2])
            url = inner[1]
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None


async def resolve_google_news_url(client: httpx.AsyncClient, url: str) -> str | None:
    if not is_google_news_url(url):
        return None
    try:
        # follow_redirects REQUIRED (Task 1 spike): the RSS article URL 302s to add
        # locale params before serving the page that carries the signature/timestamp.
        page = await client.get(
            url, headers={"User-Agent": _UA}, timeout=15.0, follow_redirects=True
        )
        if page.status_code != 200:
            return None
        sig = _SIG_RE.search(page.text)
        ts = _TS_RE.search(page.text)
        if not sig or not ts:
            return None
        resp = await client.post(
            _BATCH_ENDPOINT,
            data={"f.req": _build_freq(_article_id(url), ts.group(1), sig.group(1))},
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": _UA,
            },
            timeout=15.0,
        )
        if resp.status_code != 200:
            return None
        return _extract_publisher_url(resp.text)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError, IndexError, KeyError):
        log.warning("google news url resolution failed for %s", url, exc_info=True)
        return None
