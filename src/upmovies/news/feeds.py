"""Static feed source config for the news pipeline: trade RSS/Atom feeds plus a few
broad Google News RSS queries.

Seam: this lives in code for now so it ships with the app and is trivially testable.
If the source list grows or needs per-source tuning, it can move to a DB table (e.g.
`news.feed_source`) without changing the fetcher — the fetcher only needs `(name, url)`."""

from typing import NamedTuple


class FeedSource(NamedTuple):
    name: str  # stored as `story.source`
    url: str


# Google News RSS search feeds (broad, casting/release/trailer/greenlight signals).
def _google_news(query: str) -> str:
    from urllib.parse import quote

    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


_TRADE_FEEDS: tuple[FeedSource, ...] = (
    FeedSource("Deadline", "https://deadline.com/feed/"),
    FeedSource("Variety", "https://variety.com/feed/"),
    FeedSource("The Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
    FeedSource("Collider", "https://collider.com/feed/"),
    FeedSource("/Film", "https://www.slashfilm.com/feed/"),
    # Empire's old /movies/news/feed/ path 404s now; the live feed is Bauer Media's
    # central aggregator (and it 403s any client without an identifying User-Agent).
    FeedSource(
        "Empire",
        "https://rss.onebauer.media/api/feed-aggregator?hostname=https://www.empireonline.com",
    ),
    FeedSource("ScreenRant", "https://screenrant.com/feed/"),
)

_GOOGLE_QUERIES: tuple[tuple[str, str], ...] = (
    ("Google News: casting", "movie casting"),
    ("Google News: release date", "movie release date"),
    ("Google News: trailer", "movie trailer"),
    ("Google News: greenlight", "movie greenlight"),
)


def feed_sources(recency_days: int) -> tuple[FeedSource, ...]:
    google = tuple(
        FeedSource(name, _google_news(f"{query} when:{recency_days}d"))
        for name, query in _GOOGLE_QUERIES
    )
    return _TRADE_FEEDS + google
