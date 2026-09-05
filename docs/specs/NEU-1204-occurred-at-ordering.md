# NEU-1204 — Order events by occurred_at within days; regroup the film page on occurred_at

## Problem

Two public surfaces display events under day headings. Both group by `Event.created_at` (when the
sweep published the card) and order within a day by `created_at`:

- **The grouped feed** (`get_feed_grouped`, `public/service.py:637`) — day key is
  `cast(func.timezone("UTC", Event.created_at), Date)`; within-day events come back ordered by
  `Event.created_at.desc()` (`service.py:707`).
- **The film detail page** (`get_film_detail`, `public/service.py:300`) — day key is
  `event.created_at.astimezone(UTC)` (`service.py:371`); events ordered by
  `Event.created_at.asc(), Event.id.asc()` (`service.py:329`).

ADR-0016 set `created_at` as the axis for *every* surface and explicitly rejected grouping by
`occurred_at`. Its rationale, however, is feed-shaped — "the front page is a publication log",
"scatter backwards into dates a returning reader has already scrolled past", "publication is the
moment there is something to tell a reader about". That rationale does not extend to a single
film's own timeline, which the ADR never separately argued.

The defect became visible after NEU-1200 (credit removal events) + NEU-1201 (day_groups split).
Removal cards are frequently published days after the TMDB change they describe (the backfill
carded Aug 25 detachments on Aug 28), and a single sweep run cards multiple events sharing one
`created_at`. On Black Panther 3 (tmdb 1386618), Maya Boyd's four credit changes render as:

```
Aug 29  — joins (occurred Aug 27), departs (occurred Aug 28)   ← within-day order illogical
Aug 28  — departs (occurred Aug 25)                            ← heading Aug 28, event happened Aug 25
Aug 24  — joins  (occurred Aug 24)
```

Two defects: (1) within-day order is illogical (depart before join on Aug 29 — impossible), and
(2) the film page files an Aug 25 event under the "Aug 28" heading.

## Decision

The two surfaces answer different questions and get different axes.

- **The feed is a publication log** (unchanged from ADR-0016). Day grouping stays `created_at`.
  *Within-day* event ordering changes to `occurred_at` so events within one publication batch
  read in true chronological order.
- **The film page is an event log.** Day grouping moves to `occurred_at` (an Aug 25 departure
  lands under "Aug 25"). Within-day ordering is `occurred_at` (same rule as the feed).

The same event can now appear under different day headings on the two surfaces — e.g. the Aug 25
Maya Boyd depart shows under "Aug 28" on the feed (publication day) but "Aug 25" on the film page
(occurrence day). This is the intended consequence of "feed = publication log, film page = event
log" and is recorded as deliberate in the ADR-0016 amendment.

**No DTO change.** `EventOut` keeps its current shape (no `occurred_at` field). Backend pre-sorts
both surfaces; the frontend renders within-day events in API order on both surfaces (confirmed:
`EventTimeline.tsx:31-44` and `FeedDayCard.tsx:22-28` are plain `.map()`, no sort/reverse), so no
frontend change is needed.

## Scope

- **Backend only.** The frontend renders within-day events in API order on both surfaces (plain
  `.map()`, no sort/reverse — confirmed in the sibling repo's `EventTimeline.tsx:31-44` and
  `FeedDayCard.tsx:22-28`), so a backend-only ordering change propagates with zero frontend work.
- **Two live surfaces:** `get_feed_grouped` (within-day ordering only; day grouping unchanged)
  and `get_film_detail` (day grouping + within-day ordering).
- **Out of scope:** the flat `/feed` endpoint (`get_feed`, `service.py:577`) — unused by the
  frontend (only `/feed/grouped` is called, via `getFeedGrouped` in `public.ts:43`). Left
  untouched; noted as a known inconsistent surface.
- **Out of scope: `EventOut.occurred_at`.** ADR-0016's residual ("disclose `occurred_at` on the
  card") stays open for a future per-card-display ticket. With the film page grouped on
  `occurred_at`, the day heading already is the occurred date — per-card disclosure is redundant
  there, and the feed's heading is deliberately publication-day.

## Acceptance criteria

### Film detail page (`get_film_detail`)

- Day groups are keyed by the UTC date of `Event.occurred_at`, not `Event.created_at`.
- Within each `DayGroup`, `news_events` and `tmdb_events` are each ordered by
  `occurred_at ASC, created_at ASC, id ASC`.
- `DayGroup` entries are ordered newest-day-first (descending `occurred_at` date).
- A backfilled removal (e.g. `occurred_at` Aug 25, `created_at` Aug 28) appears under the "Aug 25"
  heading on the film page, not "Aug 28".
- The `_has_story()` split into `news_events` / `tmdb_events` is unchanged (NEU-1201's contract).

### Grouped feed (`get_feed_grouped`)

- Day grouping stays `created_at` (ADR-0016 unchanged for the feed).
- Within each (film, day) group, events in both `news_events` and `tmdb_events` subgroups are
  ordered by `occurred_at ASC, created_at ASC, id ASC`.
- The (film, day) row ordering stays `day.desc(), title.asc(), slug.asc()` — only the event
  ordering within a row changes.

### Both surfaces

- `EventOut` shape is unchanged (no `occurred_at` field added).
- No frontend change is required; the frontend's within-day render is API order on both surfaces.

### Docs

- ADR-0016 is amended with a dated NEU-1204 note: film-detail surface carves out to `occurred_at`
  grouping (event log, not publication log); feed within-day ordering moves to `occurred_at` (day
  grouping stays `created_at`).
- CONTEXT.md "Day-grouped events" entry is updated: film page groups by `occurred_at`, feed by
  `created_at`; both order within-day by `occurred_at`.

## Technical decisions

### 1. Within-day ordering: `occurred_at ASC, created_at ASC, id ASC`

`occurred_at` is the primary key (true event order). `created_at` is the secondary key so a future
per-card display has a meaningful publication timestamp, and `id` is the final deterministic anchor.
This applies to both surfaces, both subgroups (`news_events`, `tmdb_events`).

### 2. Film page day key: `occurred_at` (UTC date)

`event.occurred_at.astimezone(UTC)` → date, replacing `event.created_at.astimezone(UTC)` at
`service.py:371`. The `DayGroup.heading` derivation (`_day_heading`) is unchanged — it takes the
day key, whichever timestamp it came from.

### 3. Film page query ORDER BY

The event query at `service.py:329` changes from `Event.created_at.asc(), Event.id.asc()` to
`Event.occurred_at.asc(), Event.created_at.asc(), Event.id.asc()`. The day-group iteration
(`sorted(day_events, reverse=True)` at `service.py:376`) sorts on the new `occurred_at`-derived day
key, so newest-day-first is preserved.

### 4. Feed within-day ordering

The event fetch at `service.py:687-708` currently orders by `Event.created_at.desc()`. Change to
`Event.occurred_at.asc(), Event.created_at.asc(), Event.id.asc()`. The events are then bucketed
into `news_events_by_film_day` / `catalog_events_by_film_day` dicts (`service.py:727-751`) in
iteration order, so ASC ordering is preserved into the `EventOut` lists. The day grouping query
(`service.py:645`, `cast(func.timezone("UTC", Event.created_at), Date)`) is unchanged.

### 5. No DTO change, no frontend change

`EventOut` (`public/dto.py:14`) keeps its current fields. The frontend renders within-day events
in API order on both surfaces (confirmed: `EventTimeline.tsx:31-44` and `FeedDayCard.tsx:22-28` are
plain `.map()`). Backend pre-sorting is authoritative.

### 6. Cross-surface divergence is deliberate

The same event may show under different day headings on the feed vs the film page. This is the
direct consequence of "feed = publication log, film page = event log" and is recorded in the
ADR-0016 amendment.

## Non-goals / out of scope

- **Flat `/feed` endpoint.** Unused by the frontend; left on `created_at` ordering. Noted as a
  known inconsistent surface.
- **`EventOut.occurred_at` field.** ADR-0016's residual per-card disclosure stays open for a
  future ticket.
- **Frontend changes.** None required; both surfaces render within-day events in API order.
- **Notification trigger.** Still keys off `created_at` (ADR-0016); unaffected by how either
  display surface groups or orders.

## Tests

### `tests/integration/public/test_film_detail.py` (or equivalent)

- `test_film_detail_day_grouped_by_occurred_at` — two events with `occurred_at` on different days
  but the same `created_at` → two `DayGroup` entries keyed on `occurred_at` date.
- `test_film_detail_backfilled_removal_under_occurrence_day` — an event with `occurred_at` Aug 25
  and `created_at` Aug 28 → appears under the "Aug 25" day group, not "Aug 28".
- `test_film_detail_within_day_ordered_by_occurred_at` — two events on the same `occurred_at` day
  with `occurred_at` 08:00 and 20:00 → ordered 08:00 first, 20:00 second, regardless of
  `created_at`.
- `test_film_detail_within_day_tiebreak_created_at_then_id` — two events with the same
  `occurred_at` → ordered by `created_at` then `id`.
- `test_film_detail_day_groups_newest_first` — day groups ordered newest `occurred_at` day first.
- `test_film_detail_has_story_split_unchanged` — the `news_events` / `tmdb_events` split still
  follows `_has_story()` (NEU-1201 contract preserved).
- Existing film-detail tests (cast, crew, release dates) still pass.

### `tests/integration/public/test_feed_grouped.py` (or equivalent)

- `test_feed_grouped_day_key_still_created_at` — day grouping unchanged (`created_at`); an event
  with `occurred_at` Aug 25 and `created_at` Aug 28 still appears under the "Aug 28" day.
- `test_feed_grouped_within_day_ordered_by_occurred_at` — two events on the same `created_at` day
  with `occurred_at` 08:00 and 20:00 → ordered 08:00 first within the (film, day) row.
- `test_feed_grouped_within_day_tiebreak_created_at_then_id` — same `occurred_at` → `created_at`
  then `id`.
- `test_feed_grouped_film_row_order_unchanged` — (film, day) rows still ordered by
  `day.desc(), title.asc(), slug.asc()`.
- Existing feed-grouped tests still pass.

## Implementation plan

### Backend — `get_film_detail` (`public/service.py:300-385`)

1. Change the event query `ORDER BY` (`service.py:329`) from
   `Event.created_at.asc(), Event.id.asc()` to
   `Event.occurred_at.asc(), Event.created_at.asc(), Event.id.asc()`.
2. Change the day-key derivation (`service.py:371`) from
   `event.created_at.astimezone(UTC)` to `event.occurred_at.astimezone(UTC)`.
3. The `sorted(day_events, reverse=True)` at `service.py:376` now sorts on `occurred_at`-derived
   day keys — newest-day-first is preserved.

### Backend — `get_feed_grouped` (`public/service.py:637-751`)

1. The day grouping query (`service.py:645`, `cast(func.timezone("UTC", Event.created_at), Date)`)
   is **unchanged**.
2. Change the event fetch `ORDER BY` (`service.py:707`) from `Event.created_at.desc()` to
   `Event.occurred_at.asc(), Event.created_at.asc(), Event.id.asc()`.
3. The bucketing into `news_events_by_film_day` / `catalog_events_by_film_day`
   (`service.py:727-751`) preserves iteration order, so ASC ordering flows into the `EventOut`
   lists.

### Docs

1. Amend ADR-0016 with a dated NEU-1204 note (film-detail carve-out + feed within-day ordering).
2. Update CONTEXT.md "Day-grouped events" entry.

## Related

- NEU-1200 — credit removal events (made the backfill spike that exposed the defect)
- NEU-1201 — introduced `day_groups` with `created_at` grouping (reversed for the film page here)
- ADR-0016 — `created_at` as the publication axis (amended here: film page carves out to
  `occurred_at`; feed within-day ordering moves to `occurred_at`)
