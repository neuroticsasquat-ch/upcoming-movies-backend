from upmovies.news.models import Story

SOURCE_CAP = 3


def outlet_label(story: Story) -> str:
    """Display label for a source: the resolved publisher when present (Google News
    rows), otherwise the stored `source` (trade feeds, or unresolved rows)."""
    return story.outlet or story.source


def _dedupe_key(label: str) -> str:
    """Normalized key deciding which citations count as the same outlet. Display
    still uses the verbatim label; only the dedupe comparison is normalized:
    casefold, collapse internal whitespace, and drop a leading "the "."""
    norm = " ".join(label.casefold().split())
    if norm.startswith("the "):
        norm = norm[4:]
    return norm


def cap_sources(stories: list[Story], cap: int = SOURCE_CAP) -> list[Story]:
    """Most-recent distinct outlets, newest-first, capped to `cap`.

    Order-independent of the input: sorts internally (newest `published_at` first,
    NULL `published_at` last, story `id` as a stable tiebreak), keeps the first
    story seen per normalized outlet (= the most recent), and returns at most `cap`.
    """
    ordered = sorted(
        stories,
        key=lambda s: (
            s.published_at is None,  # non-NULL published_at first
            -(s.published_at.timestamp()) if s.published_at else 0.0,  # newest first
            str(s.id),  # stable tiebreak
        ),
    )
    seen: set[str] = set()
    result: list[Story] = []
    for story in ordered:
        key = _dedupe_key(outlet_label(story))
        if key in seen:
            continue
        seen.add(key)
        result.append(story)
        if len(result) >= cap:
            break
    return result
