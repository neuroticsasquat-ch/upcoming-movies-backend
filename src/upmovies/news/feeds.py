"""Static feed source config for the news pipeline: trade RSS/Atom feeds plus a few
broad Google News RSS queries.

Seam: this lives in code for now so it ships with the app and is trivially testable.
If the source list grows or needs per-source tuning, it can move to a DB table (e.g.
`news.feed_source`) without changing the fetcher — the fetcher only needs `(name, url)`."""

from collections.abc import Sequence
from typing import NamedTuple


class FeedSource(NamedTuple):
    name: str  # stored as `story.source`
    url: str


# Google News RSS search feeds (broad, casting/release/trailer/greenlight signals).
def _google_news(query: str) -> str:
    from urllib.parse import quote

    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


_TRADE_FEEDS: tuple[FeedSource, ...] = (
    FeedSource("Deadline", "https://deadline.com/v/film/feed/"),
    FeedSource("Variety", "https://variety.com/v/film/feed/"),
    FeedSource("The Hollywood Reporter", "https://www.hollywoodreporter.com/c/movies/feed/"),
    FeedSource("ScreenRant", "https://screenrant.com/feed/"),
    # NEU-717 additions — each verified live to return parseable RSS with the bot UA.
    # IndieWire's `/c/news/` feed is the live one; `/c/film/` is abandoned (stale since 2024).
    FeedSource("IndieWire", "https://www.indiewire.com/c/news/feed/"),
    FeedSource("Filmmaker Magazine", "https://filmmakermagazine.com/feed/"),
    FeedSource("The Wrap", "https://www.thewrap.com/creative-content/movies/feed/"),
    # Screen Daily's broad "Latest" feed — no film-only feed exists; the linker drops
    # non-matching items downstream, so the extra breadth is harmless.
    FeedSource("Screen Daily", "https://www.screendaily.com/45187.rss"),
)

_GOOGLE_QUERIES: tuple[tuple[str, str], ...] = (
    ("Google News: casting", "movie casting"),
    ("Google News: release date", "movie release date"),
    ("Google News: trailer", "movie trailer"),
    ("Google News: greenlight", "movie greenlight"),
)


def feed_sources(recency_days: int, *, google_enabled: bool) -> tuple[FeedSource, ...]:
    """Feeds for a run's Phase A. Always the curated trade feeds; the broad Google News
    queries are appended only when `google_enabled` (NEU-717 pauses them by default)."""
    if not google_enabled:
        return _TRADE_FEEDS
    google = tuple(
        FeedSource(name, _google_news(f"{query} when:{recency_days}d"))
        for name, query in _GOOGLE_QUERIES
    )
    return _TRADE_FEEDS + google


GOOGLE_SOURCE_PREFIX = "Google News:"  # every google `story.source` label starts with this
_PER_FILM_SOURCE = f"{GOOGLE_SOURCE_PREFIX} per-film"  # constant story.source per per-film hit


def is_google_source(name: str) -> bool:
    """True for any Google News feed name (per-film search + the broad queries).
    These are aggregator labels whose stories need outlet resolution."""
    return name.startswith(GOOGLE_SOURCE_PREFIX)


def per_film_google_sources(titles: Sequence[str], recency_days: int) -> tuple[FeedSource, ...]:
    """One Google News source per tracked film, unquoted `<title> when:Nd`. Unquoted
    maximizes recall (Google quoting is fuzzy relevance-matching, not strict phrase);
    the entity linker owns precision downstream."""
    return tuple(
        FeedSource(_PER_FILM_SOURCE, _google_news(f"{title} when:{recency_days}d"))
        for title in titles
    )
