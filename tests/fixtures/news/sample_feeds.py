"""Fixture RSS/Atom payloads for the feed-fetcher tests. No network — these are fed
straight to the parser or served via respx."""

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Deadline</title>
    <item>
      <title>Big Movie Casts a Star</title>
      <link>https://deadline.com/2026/06/story-1</link>
      <pubDate>Tue, 10 Jun 2026 14:30:00 GMT</pubDate>
      <description>A summary of the casting news.</description>
    </item>
    <item>
      <title>Sequel Gets a Greenlight</title>
      <link>https://deadline.com/2026/06/story-2</link>
      <pubDate>Wed, 11 Jun 2026 09:00:00 GMT</pubDate>
      <description>The studio moves forward.</description>
    </item>
  </channel>
</rss>
"""

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Variety</title>
  <entry>
    <title>First Trailer Drops</title>
    <link rel="alternate" href="https://variety.com/2026/film/trailer-1"/>
    <published>2026-06-12T09:00:00Z</published>
    <updated>2026-06-12T10:00:00Z</updated>
    <summary>Watch the trailer.</summary>
  </entry>
</feed>
"""

# A second item missing its <link> — the fetcher must skip it (url is NOT NULL/unique).
RSS_FEED_WITH_LINKLESS_ITEM = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>THR</title>
    <item>
      <title>Has a Link</title>
      <link>https://thr.com/has-link</link>
      <pubDate>Tue, 10 Jun 2026 14:30:00 GMT</pubDate>
    </item>
    <item>
      <title>No Link Here</title>
      <pubDate>Tue, 10 Jun 2026 15:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

# Two entries pointing at the same URL — must collapse to one row.
RSS_FEED_WITH_DUPLICATE_URLS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Collider</title>
    <item>
      <title>Story A</title>
      <link>https://collider.com/dupe</link>
      <pubDate>Tue, 10 Jun 2026 14:30:00 GMT</pubDate>
    </item>
    <item>
      <title>Story A (reposted)</title>
      <link>https://collider.com/dupe</link>
      <pubDate>Tue, 10 Jun 2026 16:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

MALFORMED_FEED = "this is not a feed at all <<< not xml >>>"
