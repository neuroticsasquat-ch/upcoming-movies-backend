# NEU-1199 — Split front-page feed by source category

## Problem

In the grouped feed (`/feed/grouped`), a film-day appears in exactly **one** section
("In the News" or "via TMDB"). The decision uses `news_backed` (whether *any* event
in that film-day has a story). **All** events for that film-day are bundled into
whichever section wins, so a film-day with e.g. a Variety casting story *and* a TMDB
release-date change only shows up in "In the News" with both events — it never appears
in "via TMDB" at all.

**Desired**: A film-day with events from both categories should appear in **both**
sections, each carrying only its own category's events. `event_count`, `top_event_type`,
and `event_types` are scoped to the category's subset.

## Scope

- **Only the grouped feed** (`/feed/grouped`). The flat feed (`/feed`) is one event
  per row — no change needed.
- `FeedDayItem` schema stays the same; the frontend already keys off `news_backed`.
- Single file change: `src/upmovies/public/service.py`.

## Implementation

### `get_feed_grouped()` in `service.py`

The current three-phase approach needs a tweak in phase 3:

1. **Phase 1 (aggregate SQL)** — unchanged. One row per `(film, day)` with
   `event_count`, `event_types`, `news_backed`.

2. **Phase 2 (fetch events + sources)** — add `_has_story().label("has_story")` as
   a column in the event fetch query, so each event row carries its own story signal.

3. **Phase 3 (build items)** — instead of one `FeedDayItem` per aggregate row,
   produce **two** when the film-day has both categories:

   - If any event has `has_story=True` → emit a `FeedDayItem` with `news_backed=True`
     and only those events.
   - If any event has `has_story=False` → emit a `FeedDayItem` with `news_backed=False`
     and only those events.

   `event_count`, `top_event_type`, `event_types` are computed from each category's
   own event list, not the full film-day.

## Tests

### Modified

| Test | Current expectation | After |
|------|-------------------|-------|
| `test_grouped_one_story_backed_event_flags_the_whole_film_day` | 1 item, `event_count=5`, all events | 2 items (news=1 event, catalog=4 events) |

### New

- `test_grouped_same_film_day_in_both_sections` — 1 story event + 1 catalog event
  → 2 items, same `film_ref` and `day`, different `news_backed`.
- `test_grouped_split_event_count_and_types_are_scoped` — multiple events per
  category, verify `event_count` and `top_event_type` are category-scoped.
- `test_grouped_promoted_then_split` — catalog event that gains a story stays in
  news-backed, remaining catalog-only events stay in catalog section.
