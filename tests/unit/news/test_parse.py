from datetime import UTC, datetime

from tests.fixtures.news.sample_feeds import (
    ATOM_FEED,
    MALFORMED_FEED,
    RSS_FEED,
    RSS_FEED_WITH_LINKLESS_ITEM,
)
from upmovies.news.fetcher import parse_feed


def test_parse_rss_returns_normalized_entries():
    entries = parse_feed("Deadline", RSS_FEED)
    assert [e.url for e in entries] == [
        "https://deadline.com/2026/06/story-1",
        "https://deadline.com/2026/06/story-2",
    ]
    first = entries[0]
    assert first.source == "Deadline"
    assert first.title == "Big Movie Casts a Star"
    assert first.published_at == datetime(2026, 6, 10, 14, 30, tzinfo=UTC)
    assert first.raw  # raw payload retained


def test_parse_atom_uses_link_href_and_published():
    entries = parse_feed("Variety", ATOM_FEED)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.url == "https://variety.com/2026/film/trailer-1"
    assert entry.title == "First Trailer Drops"
    assert entry.published_at == datetime(2026, 6, 12, 9, 0, tzinfo=UTC)


def test_parse_skips_entries_without_a_url():
    entries = parse_feed("THR", RSS_FEED_WITH_LINKLESS_ITEM)
    assert [e.url for e in entries] == ["https://thr.com/has-link"]


def test_parse_malformed_feed_returns_empty_list():
    assert parse_feed("Whatever", MALFORMED_FEED) == []
