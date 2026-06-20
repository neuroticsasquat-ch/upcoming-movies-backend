from datetime import UTC, datetime

from upmovies.news.fetcher import StoryEntry, drop_stale


def _entry(url: str, published_at: datetime | None) -> StoryEntry:
    return StoryEntry(source="X", url=url, title="t", published_at=published_at, raw={})


def test_drop_stale_keeps_recent_and_undated_drops_old():
    cutoff = datetime(2026, 6, 6, tzinfo=UTC)
    recent = _entry("https://e/recent", datetime(2026, 6, 10, tzinfo=UTC))
    at_cutoff = _entry("https://e/at", datetime(2026, 6, 6, tzinfo=UTC))  # boundary: kept (>=)
    old = _entry("https://e/old", datetime(2026, 1, 1, tzinfo=UTC))
    undated = _entry("https://e/undated", None)

    kept = drop_stale([recent, at_cutoff, old, undated], cutoff=cutoff)

    assert kept == [recent, at_cutoff, undated]
