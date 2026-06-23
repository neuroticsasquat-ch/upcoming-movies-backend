"""Resolve the real publisher name for Google News stories.

Google News RSS items carry the actual outlet in their `<source>` element, which
feedparser exposes as `entry.source.title`. When that is absent, the item title
arrives as "Headline - Publisher" and the trailing segment is the outlet. Both
paths are zero-network — we never follow the (encoded, fragile) Google redirect URL.
"""

from typing import Any


def outlet_from_title(title: str) -> str | None:
    """Extract the publisher from a "Headline - Publisher" Google News title.

    Splits on the LAST " - " so headlines containing earlier dashes still resolve to
    the trailing publisher. Returns None when there is no separator or the trailing
    segment is empty/whitespace.
    """
    _head, sep, tail = title.rpartition(" - ")
    if not sep:
        return None
    return tail.strip() or None


def outlet_from_entry(entry: Any) -> str | None:
    """Resolve a Google News item's outlet: prefer the RSS `<source>` element
    (`entry.source.title`), then fall back to the title suffix. `entry` is a
    feedparser entry (dict-like)."""
    source = entry.get("source")
    name = source.get("title") if isinstance(source, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return outlet_from_title(entry.get("title") or "")
