# NEU-1208 — Feed "via TMDB" section lists movie titles only; rename to "unconfirmed updates"; drop per-card attribution

## Problem

TMDB credits oscillate (added → removed → added → removed over a few days) and early TMDB
updates are generally unreliable — TMDB is community-edited, so a credit can be added
speculatively and later reverted. NEU-1205 dampened the oscillation at the source (forward-dwell
gate on removal carding), and NEU-1207 retired the film-page collapse so the surviving cards are
visible. But on the **front-page feed**, catalog-sourced activity still receives the same per-event
card rendering as trade-sourced news: a full event summary line, an event-type badge, and (when no
outlet wrote about it) a "via TMDB" attribution line. That is more visibility than the unreliability
of early TMDB updates warrants, and the "via TMDB" label names the source rather than signalling
the *uncertainty* a reader should actually take away.

Three changes demote TMDB-sourced activity on the feed and sharpen the veracity signal on both
surfaces:

1. **The feed's catalog section lists movie titles only** — the film title linked to the film page,
   no event cards. A reader who wants the TMDB updates clicks through to the film page, where the
   events render inline (NEU-1207) under the renamed label.
2. **The label "via TMDB" → "unconfirmed updates"** on both the feed and the film page, so the
   heading signals the *uncertainty* of the updates rather than naming their source.
3. **The per-event "via TMDB" attribution line is removed** from every event card. The section
   heading is now the sole veracity signal; a catalog-sourced event with no outlets renders no
   attribution line at all.

This is the next step in the demotion arc: NEU-1201 collapsed the section (hide), NEU-1207 retired
the collapse on the film page (label-only demotion), and NEU-1208 retires the per-card attribution
and further reduces the feed's catalog section to titles (visibility demotion on the feed, label
renamed on both).

## Scope

- **Frontend code (both surfaces) + backend payload (feed only) + docs.** The backend stops
  shipping `events` for `news_backed=false` feed items; the frontend stops rendering events for
  catalog feed items and drops the per-card attribution on both surfaces; the label is renamed on
  both surfaces; ADR-0014 and CONTEXT.md are amended.
- **Feed and film page only.** No other surface (calendar, sitemap, film index) is touched.
- **Label rename + attribution removal are presentation-only.** No `provenance`, `news_backed`,
  `_has_story()`, or `DayGroup` semantic change. The subgrouping axis is unchanged.

## Acceptance criteria

### Feed (`/feed`, grouped) — `src/routes/feed.tsx`, `src/components/feed/FeedDayCard.tsx`

- **Both section labels appear on every day.** A day always renders two labelled sections —
  "In the news" and "unconfirmed updates" — regardless of whether one is empty. The current
  single-subgroup unlabelled-list path (`feed.tsx:74-80`, `label: null` branch) is removed. This
  is a structural change: today a news-only day renders as an unlabelled list; under NEU-1208 it
  renders an "In the news" heading plus an empty "unconfirmed updates" section.
- **Empty section renders a static "None today" line** (no toggle, no count suffix), expanded and
  visible without a click. So a catalog-only day shows "In the news — None today" (static) then
  "unconfirmed updates (N movies)" (collapsed toggle); a news-only day shows "In the news" (full
  cards) then "unconfirmed updates — None today" (static).
- **Non-empty "unconfirmed updates" section stays collapsed by default** (Q2 — the
  `SectionWrapper` `useState(section.key !== "tmdb")` toggle is kept), with the `(N movies)` count
  suffix. The section key stays `"tmdb"` (the collapse logic keys on it; only the label text
  changes).
- **"In the news" section is always expanded** (unchanged), with the `(N movies)` count suffix.
- **Catalog-section rows render the movie title only** — the `<Link to={`/film/${film_ref}`}>`
  title + year/arc-stage (`FeedDayCard.tsx:12-21`) — with **no event list**. The
  `{item.events.length > 0 && (...)}` block (`FeedDayCard.tsx:22-28`) does not render because
  catalog items ship `events=[]` (see Backend below). `FeedDayCard` itself needs no change beyond
  the `FeedEventSources` fallback removal (Q5): the existing `events.length > 0` guard naturally
  suppresses the event list when `events` is empty.
- **News-section rows render unchanged** — full event cards with summaries, badges, and source
  chips. News items still ship full `events`.
- **A film-day appearing in both sections** (NEU-1199: one `news_backed=true` row + one
  `news_backed=false` row for the same film) renders the title in both sections: full cards under
  "In the news", title-only under "unconfirmed updates". This is the existing NEU-1199 contract,
  unchanged.

### Film page (`/film/{ref}`) — `src/components/film/EventTimeline.tsx`, `src/components/film/SourceLinks.tsx`

- **The `TmdbSubSection` heading renames "via TMDB" → "unconfirmed updates".** The section stays
  inline/uncollapsed (NEU-1207, unchanged) — only the `<h4>` text changes.
- **Subgroup headings remain conditional** (Q8 — feed only gets both-always; the film page keeps
  rendering a subgroup heading only when that subgroup is non-empty). A day with only news events
  renders "In the news" and no "unconfirmed updates" heading; a day with only TMDB events renders
  "unconfirmed updates" and no "In the news" heading. No "None today" on the film page.
- **Per-event "via TMDB" attribution removed.** `SourceLinks.tsx:26-29` (the
  `sources.length === 0 && provenance === "catalog"` → `<p>via TMDB</p>` fallback) is replaced
  with `if (sources.length === 0) return null;`. A catalog-sourced event with no outlets renders
  no attribution line. The `admin`/`onDelink` path (`SourceLinks.tsx:57-67`) is unaffected — it
  only renders delink buttons for *sourced* events, and catalog events have no sources.
- **The doc comment** at `SourceLinks.tsx:8-12` (which justifies the "via TMDB" fallback) is
  updated to reflect the removal.

### Label constant — `src/components/film/labels.ts`

- A shared constant (e.g. `UNCONFIRMED_UPDATES_LABEL = "unconfirmed updates"`) is introduced in
  `labels.ts` and referenced from both production label sites (`feed.tsx:78`, `EventTimeline.tsx:11`).
  The literal "via TMDB" no longer appears in any production source file. The "In the news" label
  is unchanged.

### Backend — `src/upmovies/public/service.py`, `src/upmovies/public/dto.py`

- **`get_feed_grouped` stops shipping `events` for `news_backed=false` items.** The
  `_make_item(row, events, news_backed)` call for catalog items passes `events=[]` while keeping
  `event_count`, `top_event_type`, and `event_types` computed from the real `catalog_events` list.
  News items are untouched (full `events`). No DTO change — `FeedDayItem.events` already defaults
  to `[]` (`dto.py:148`); no migration.
- **`event_count` stays accurate** for catalog items (`= len(catalog_events)`) so the count badge
  is correct. `top_event_type` / `event_types` stay populated from `catalog_events` (correctly
  describe the day's catalog activity; unused in the UI but harmless and less surprising than
  empty aggregates beside a non-zero count).

### Docs — ADR-0014, CONTEXT.md

- **ADR-0014** gets a dated NEU-1208 amendment recording: the "via TMDB" label is renamed to
  "unconfirmed updates" on both surfaces; the per-card "via TMDB" attribution is removed (the
  section heading is now the sole veracity signal); the feed's catalog section is further demoted
  to title-only links (events are no longer rendered on the feed for catalog items, and the
  backend no longer ships them). The film-page label-only demotion (NEU-1207) is unchanged in
  kind — only the label text moves.
- **CONTEXT.md** entries referencing "via TMDB" are updated: "Day-grouped events" (label renamed,
  feed section now titles-only), "Catalog-sourced event" (per-card attribution removed, label
  renamed), "Credit attachment event" and "Credit detachment event" (section renamed), "Credit
  oscillation" (section renamed; the transient-invariant confinement now holds for the feed's
  titles-only collapsed section and the film page's inline section per NEU-1207).

### Tooling

- `task test && task lint && task typecheck` pass in **both** repos (backend: no code change beyond
  `service.py`, existing tests adjusted; frontend: tests updated for the rename, titles-only
  rendering, and attribution removal). Changed/new files are Prettier-formatted (frontend) and
  Ruff-formatted (backend).

## Technical decisions

### 1. Both labels always, "None today" for empty — feed only (Q1, Q8, Q10)

The feed always renders two labelled sections per day. This is a structural change from the current
"single subgroup = unlabelled list" rule (`feed.tsx:74-80`): a news-only day that today renders as a
bare list now renders an "In the news" heading plus an empty "unconfirmed updates — None today"
section. The rationale: a consistent two-section structure aids scanning a publication log, and
the "None today" line makes the absence of catalog activity legible rather than a silent gap. The
film page is **not** changed this way — it keeps conditional headings (render a subgroup heading
only when non-empty), because a per-film event log with many single-category days would accumulate
noise from empty "None today" lines under most day headings.

### 2. Empty section: static "None today", no toggle, no count suffix (Q10)

An empty section renders the label without the `(N movies)` suffix (the count is zero — redundant
next to "None today") and as a static line with no collapse toggle, so "None today" is visible
without a click. A non-empty "unconfirmed updates" section keeps the collapsed toggle with the
`(N movies)` suffix (Q2 — the collapse is the strongest "less visibility" and NEU-1207 kept it).
"In the news" is always expanded either way (unchanged).

### 3. Catalog items ship `events=[]`; `FeedDayCard` needs no render change (Q6b, Q9)

The backend stops populating `events` for `news_backed=false` items but keeps `event_count` (and
the event-type aggregates) accurate. Because `FeedDayCard` already guards the event list behind
`item.events.length > 0` (`FeedDayCard.tsx:22`), an empty `events` array naturally suppresses the
event cards — so `FeedDayCard` needs no conditional rendering change for the titles-only mode; the
existing guard does the work. The only `FeedDayCard` change is removing the `FeedEventSources`
"via TMDB" fallback (Q5), which is now dead code for the feed regardless (catalog items have no
events to render; news items have sources).

### 4. Per-card attribution removed on both surfaces (Q5)

Two fallback implementations render `<p>via TMDB</p>` when `sources.length === 0 && provenance ===
"catalog"`: `SourceLinks.tsx:26-29` (film page, used by `EventCard`) and `FeedDayCard.tsx:59-62`
(feed, `FeedEventSources`). Both are replaced with `return null` on empty sources. The section
heading ("unconfirmed updates") is the sole veracity signal. The `admin`/`onDelink` path in
`SourceLinks` only renders delink buttons for *sourced* events (`SourceLinks.tsx:57-67`); catalog
events have no sources, so the removed fallback never fed it — admin delink is unaffected.

### 5. Shared label constant (Q11)

The literal "via TMDB" appears in 4 production sites (2 section headings + 2 attribution
fallbacks). The attribution fallbacks are removed (Q5); the 2 section headings reference a single
`UNCONFirmed_UPDATES_LABEL` constant in `labels.ts`. "In the news" stays a literal (it is not
renamed and appears in 2 heading sites). After NEU-1208, no production source file contains the
string "via TMDB".

### 6. ADR-0014 amendment + CONTEXT.md update (Q7)

Renaming a label referenced across three ADR amendments (NEU-1200, NEU-1205, NEU-1207 all lean on
"via TMDB") and removing the per-card attribution (ADR-0014's "Presentation" rule: "attributes to
'via TMDB' in place of outlets") is hard to reverse, surprising without context, and a real
trade-off (section label carries veracity alone, no per-card signal) — meets all three ADR bars.
Follows NEU-1207's exact pattern (dated amendment to ADR-0014 + CONTEXT.md entries updated).

## Implementation plan

### Frontend — feed (`src/routes/feed.tsx`)

1. Replace the `sections` ternary (`feed.tsx:74-80`) to **always** emit two sections —
   `{ key: "news", label: "In the news", items: newsBacked }` and
   `{ key: "tmdb", label: UNCONFIRMED_UPDATES_LABEL, items: tmdbOnly }` — removing the
   `label: null` unlabelled-list branch.
2. `SectionWrapper` (`feed.tsx:127-174`): add an empty-section path. When `section.items.length
   === 0`, render the label (no count suffix) + a static "None today" line, no toggle, expanded.
   The existing news/TMDB branches handle non-empty sections (news expanded, TMDB collapsed with
   count suffix). The `useState(section.key !== "tmdb")` hook stays (Q2 — non-empty TMDB section
   is still collapsed); for an empty TMDB section the toggle is not rendered so the hook's value
   is moot, but the hook must still be called unconditionally (lint rule — keep it before any
   early return, as today).

### Frontend — feed card (`src/components/feed/FeedDayCard.tsx`)

1. Remove the `FeedEventSources` fallback (`FeedDayCard.tsx:59-62`): replace the
   `sources.length === 0` branch with `return null`. The `provenance` parameter becomes unused —
   drop it from the `FeedEventSources` signature and the `FeedEvent` call site (`FeedDayCard.tsx:47`).
   If `FeedEventSources` reduces to "render source chips or nothing", it may be inlined or kept;
   the `provenance` prop is no longer needed.
2. `FeedDayCard`'s event-list block (`FeedDayCard.tsx:22-28`) is **unchanged** — the
   `item.events.length > 0` guard naturally renders title-only when the backend ships `events=[]`
   for catalog items.

### Frontend — film page (`src/components/film/EventTimeline.tsx`)

1. `TmdbSubSection` heading (`EventTimeline.tsx:11`): replace the literal `"via TMDB"` with the
   shared `UNCONFIRMED_UPDATES_LABEL` constant. The section stays inline/uncollapsed (no toggle).
2. The conditional-heading structure (`EventTimeline.tsx:44-64`) is **unchanged** — the film page
   keeps rendering a subgroup heading only when non-empty (Q8). No "None today" on the film page.

### Frontend — per-event attribution (`src/components/film/SourceLinks.tsx`)

1. Replace the `sources.length === 0` branch (`SourceLinks.tsx:26-29`) with
   `if (sources.length === 0) return null;`. Drop the `provenance` parameter (no longer read).
2. Update the doc comment (`SourceLinks.tsx:8-12`) to remove the "attributes to TMDB" rationale.
3. Update `EventCard.tsx:54-64` to stop passing `provenance` to `SourceLinks` (now unused).

### Frontend — label constant (`src/components/film/labels.ts`)

1. Add `export const UNCONFIRMED_UPDATES_LABEL = "unconfirmed updates";` (lowercase, matching the
   existing "In the news" / "via TMDB" sentence-case style).
2. Reference it from `feed.tsx` (the `sections` array) and `EventTimeline.tsx` (the
   `TmdbSubSection` heading).

### Backend — feed payload (`src/upmovies/public/service.py`)

1. In `_make_item` (`service.py:757-770`), add a `ship_events: bool` parameter (or split the
   constructor call): for `news_backed=False`, pass `events=[]` while keeping `event_count`,
   `top_event_type`, `event_types` computed from the real `catalog_events` list. For
   `news_backed=True`, pass the full `events` (unchanged).
2. The `items.append` loop (`service.py:772-780`): the catalog call (`service.py:779`) passes
   `events=[]` (or `ship_events=False`); the news call (`service.py:778`) is unchanged.
3. No DTO change (`FeedDayItem.events` defaults to `[]`); no migration.

### Backend — docs (ADR-0014, CONTEXT.md)

1. Add a dated NEU-1208 amendment to `docs/adr/0014-catalog-sourced-events.md`: label renamed to
   "unconfirmed updates"; per-card "via TMDB" attribution removed (section heading is the sole
   veracity signal); feed catalog section demoted to title-only links (backend no longer ships
   `events` for `news_backed=false` items); film-page label-only demotion (NEU-1207) unchanged in
   kind. Note the interaction with the NEU-1205 transient-invariant "confined to the collapsed
   section" argument: on the feed it is now confined to the titles-only collapsed
   "unconfirmed updates" section (still collapsed, still low-harm); on the film page it remains
   visible-by-default per NEU-1207.
2. Update CONTEXT.md entries: "Day-grouped events" (label renamed, feed section titles-only),
   "Catalog-sourced event" (attribution removed, label renamed), "Credit attachment event" and
   "Credit detachment event" (section renamed), "Credit oscillation" (section renamed; transient
   confinement now the feed's titles-only collapsed section / film page's inline section).

## Tests

### Frontend — feed (`src/routes/feed.test.tsx`)

- **Update** existing "via TMDB" assertions to "unconfirmed updates" (lines 319, 334, 358, 364,
  365, 377, 385, 405 — assert the new label text).
- **Update** `renders both sections when both present` — both headings present ("In the news" +
  "unconfirmed updates"); the unconfirmed section is collapsed by default; the news section is
  expanded.
- **Add** `renders both labels on a news-only day` — a day with only news-backed items renders
  "In the news" (full cards) + "unconfirmed updates — None today" (static, no toggle, no count
  suffix).
- **Add** `renders both labels on a tmdb-only day` — a day with only catalog items renders
  "In the news — None today" (static) + "unconfirmed updates (N movies)" (collapsed toggle).
- **Add** `tmdb section renders movie titles only without event cards` — a catalog-section item
  renders the title link to `/film/{film_ref}` and does **not** render event summaries or
  event-type badges (assert the summary text is absent).
- **Add** `empty section shows none today without count suffix` — an empty section's label does
  not include "(0 movies)" and the body contains "None today".
- **Keep** the within-day ordering, poster-strip, and "view more" tests unchanged.

### Frontend — feed card (`src/components/feed/FeedDayCard.test.tsx`)

- **Add** `renders title only when events empty` — a `FeedDayItem` with `events=[]` renders the
  title link and no event block (the existing `event_count: 3, events: []` fixture at line 91
  already exercises this; assert no event summary text).
- **Update** any test asserting "via TMDB" on a feed event — the attribution line is gone; assert
  no "via TMDB" text renders for a catalog event with empty sources.

### Frontend — film page (`src/components/film/EventTimeline.test.tsx`)

- **Update** "via TMDB" assertions (lines 117, 133, 134, 149) to "unconfirmed updates".
- **Keep** the NEU-1207 inline/uncollapsed tests (no toggle button, both subgroups visible on
  initial render) — unchanged except the label text.

### Frontend — per-event attribution (`src/components/film/SourceLinks.test.tsx`)

- **Update** `renders via TMDB attribution for catalog event with no sources` (line 44-45) →
  `renders no attribution for catalog event with no sources` — assert `null` / no "via TMDB" text.
- **Keep** the sourced-event tests (lines 32, 38) unchanged — source chips and admin delink still
  render for sourced events.
- **Update** `EventCard.test.tsx` "via TMDB" assertions (lines 141, 150, 157) — no "via TMDB"
  line on a catalog event.

### Backend — feed payload (`tests/integration/public/test_feed.py` or equivalent)

- **Add** `test_grouped_catalog_item_ships_empty_events` — a `news_backed=False` item has
  `events=[]` while `event_count` matches the real catalog event count for that film-day.
- **Add** `test_grouped_news_item_still_ships_events` — a `news_backed=True` item has the full
  `events` list (unchanged).
- **Update** any test that asserts `events` is non-empty for a catalog-only film-day — it now
  expects `events=[]`.
- **Keep** the NEU-1199 split tests (same film-day in both sections) — the news item has events,
  the catalog item has `events=[]`.

## Out-of-scope reminders

- **Film page "both labels always" / "None today"** — not done (Q8; film page stays conditional).
- **"In the news" label** — unchanged.
- **Event-type badges** (`eventTypeLabel`) — unchanged; only the per-card "via TMDB" attribution
  line is removed.
- **`provenance`, `news_backed`, `_has_story()`, `DayGroup` semantics** — unchanged.
- **Per-row metadata on feed catalog rows** (count badge, event-type labels) — not added (Q3;
  bare title only). `event_count` / `top_event_type` / `event_types` stay in the payload (accurate,
  unused in the UI) but are not rendered.
- **Calendar, sitemap, film index** — untouched.
- **Dropping `events` from news items, or removing `events`/`top_event_type`/`event_types` from
  the DTO** — not done (catalog items only; DTO unchanged; no migration).
