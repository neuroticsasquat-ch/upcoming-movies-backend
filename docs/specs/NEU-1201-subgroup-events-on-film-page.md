# NEU-1201 — Subgroup events on film page by day into "in the news" and "via TMDB" sections

## Problem

The film detail page (`/films/{ref}`) returns a flat chronological list of events via `FilmDetailResponse.events`. TMDB-sourced events (catalog provenance) and trade-sourced events (story provenance / story-linked) are interleaved regardless of their source. The veracity of TMDB data is lower than that of trade publications: TMDB is community-edited, so credits can be added speculatively and later reverted. TMDB events should not receive equal prominence on a film page.

A related consideration is NEU-1200 (credit removal events), which will also affect event display; the grouping structure here is orthogonal to that work.

## Scope

**Both backend and frontend.** The backend restructures the film detail API response; the frontend renders two subgrouped sections per day.

## Acceptance criteria

- A film's events are grouped by day, with each day split into `news_events` and `tmdb_events` subgroups.
- The subgrouping axis is `_has_story()` (whether a story is currently linked via `event_story`), **not** `Event.provenance`. A catalog-born event that later gets a trade story migrates to `news_events`.
- A day with events in both subgroups renders two labelled sections: "In the news" (always expanded) and "via TMDB" (collapsed by default). This mirrors the feed's `SectionWrapper` pattern.
- A day with events in only one subgroup renders a single unlabelled section (no redundant heading).
- A day with zero events is not emitted (same as today).
- The event count shown in the "Latest updates" section heading reflects the total across both subgroups.
- Per-event cards render identically to today — `EventOut` shape is unchanged (same `provenance`, `sources`, `summary`, etc.).
- The change is backward-compatible for the frontend: the response shape changes, but the old `events` key is replaced with `day_groups`.

### Non-goals / out of scope

- **NEU-1200** (credit removal events). If that work creates a third event type or section, adjusting the film page for it is a separate ticket. The two-section structure here is designed to accommodate a third section in the future.
- **Changes to the feed** (`/feed`, `/feed/grouped`). Unchanged.
- **New API versioning.** The film detail response is consumed only by this frontend.

## Technical decisions

1. **Subgrouping axis: `_has_story()` EXISTS subquery.** Reuses the same `_has_story()` correlated EXISTS from `public/service.py:122-136`. The grouped feed already uses this contract (expressed as `news_backed` on `FeedDayItem`); the film page should agree. `provenance` is immutable (ADR-0014) and would leave a Variety-covered TMDB event permanently demoted.

2. **Backend response shape: `DayGroup` structure.** A new nested type:
   - `DayGroup`: `{ day: date, heading: str, news_events: list[EventOut], tmdb_events: list[EventOut] }`
   - `FilmDetailResponse.events` is **replaced** by `FilmDetailResponse.day_groups: list[DayGroup]`.
   - Backend groups events by `cast(func.timezone("UTC", Event.created_at), Date)`, same day-key logic as the grouped feed (ADR-0016). Within each day, events are split by the `_has_story()` predicate.
   - Within each subgroup, events retain the existing ordering (`created_at ASC, id ASC`, reversed to newest-first on the frontend).

3. **Frontend rendering: mirrored from the feed's `SectionWrapper`.** The `EventTimeline` component is replaced with a component that:
   - Iterates `film.day_groups` (newest day first from the backend).
   - For each `DayGroup`, renders a date heading (unchanged from today), then:
     - If both `news_events` and `tmdb_events` are non-empty: two labelled subsections — "In the news" (always visible) and "via TMDB" (collapsible, collapsed by default).
     - If only one is non-empty: a single unlabelled list (no redundant heading).
   - Reuses the feed's `SectionWrapper` pattern or a shared component.
   - The "Latest updates" collapsible section heading count is `sum(len(g.news_events) + len(g.tmdb_events) for g in day_groups)`.

4. **TMDB section collapsed by default.** Same as the feed (cf. `SectionWrapper` in `feed.tsx:136`). A React `useState` toggles visibility; on initial render the TMDB section is closed. This is a frontend-only concern — the backend does not signal collapse state.

5. **No `has_story` field on `EventOut`.** The split is done at the `DayGroup` level, not per-event. The frontend receives pre-grouped events.

## Implementation plan

### Backend

1. Add `DayGroup` Pydantic model to `public/dto.py`.
2. In `get_film_detail` (`public/service.py`):
   - Add `_has_story().label("has_story")` to the event select query (same pattern as the grouped feed at lines 682-706).
   - After building `list[EventOut]` and attaching sources, group events by day key, then split each day's events by `has_story` into `news_events` and `tmdb_events`.
   - Replace `events=events` in the `FilmDetailResponse` constructor with `day_groups=day_groups`.
3. Remove the `events` field from `FilmDetailResponse`; add `day_groups`.

### Frontend

1. Update `FilmDetail` type in `src/api/types.ts`: replace `events: FilmEvent[]` with `day_groups: DayGroup[]`.
2. Add `DayGroup` interface: `{ day: string; heading: string; news_events: FilmEvent[]; tmdb_events: FilmEvent[] }`.
3. Replace `<EventTimeline>` with a new component that renders per-day subgroups using the feed's `SectionWrapper`-style rendering.
4. Reuse or extract the `SectionWrapper` component from `feed.tsx` to avoid duplicating the expand/collapse logic for the TMDB section.
5. Remove the now-unnecessary `groupEventsByDay` call from the film page (the backend already groups).

## Tests

### Backend

- `test_film_detail_day_groups_structure` — 2 events on different days → 2 `DayGroup` entries.
- `test_film_detail_day_group_split` — 1 story-linked + 1 catalog-only on same day → same day key, `news_events=[story]`, `tmdb_events=[catalog]`.
- `test_film_detail_day_group_all_news` — all events story-linked → single non-empty subgroup per day.
- `test_film_detail_day_group_all_tmdb` — all events catalog-only with no stories → single non-empty subgroup per day.
- `test_film_detail_day_group_empty_day` — no events on a given day → not emitted (existing empty-films test covers this).
- Existing tests for other film detail fields (cast, crew, release dates, etc.) must still pass.

### Frontend

- `test_film_page_renders_day_groups` — mock response with 2 days, each with both subgroups → verify 2 date headings, 2 "In the news" labels, 2 "via TMDB" labels.
- `test_film_page_tmdb_section_collapsed_by_default` — TMDB section is not visible on initial render; news section is.
- `test_film_page_single_subgroup_no_label` — day with only news events → no redundant "In the news" heading.
- `test_film_page_event_count_includes_both_subgroups` — total count in "Latest updates" heading is sum of all events.
