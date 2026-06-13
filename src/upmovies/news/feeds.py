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


FEED_SOURCES: tuple[FeedSource, ...] = (
    # Trade / enthusiast RSS + Atom.
    FeedSource("Deadline", "https://deadline.com/feed/"),
    FeedSource("Variety", "https://variety.com/feed/"),
    FeedSource("The Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
    FeedSource("Collider", "https://collider.com/feed/"),
    FeedSource("/Film", "https://www.slashfilm.com/feed/"),
    FeedSource("Empire", "https://www.empireonline.com/movies/news/feed/"),
    FeedSource("ScreenRant", "https://screenrant.com/feed/"),
    # Broad Google News queries.
    FeedSource("Google News: casting", _google_news("movie casting")),
    FeedSource("Google News: release date", _google_news("movie release date")),
    FeedSource("Google News: trailer", _google_news("movie trailer")),
    FeedSource("Google News: greenlight", _google_news("movie greenlight")),
)
