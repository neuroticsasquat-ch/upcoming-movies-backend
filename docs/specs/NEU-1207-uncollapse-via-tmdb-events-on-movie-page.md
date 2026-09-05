# NEU-1207 — Uncollapse "via TMDB" events on the movie page

## Problem

The film page (`/films/{ref}`) renders each day's events in two subgroups — `news_events`
("In the news") and `tmdb_events` ("via TMDB") — introduced by NEU-1201. The "via TMDB"
section is **collapsed by default** (`EventTimeline.tsx`, `TmdbSubSection`: `useState(false)`):
a `<button>` toggles visibility, and the events render only once opened.

When a day has **only** TMDB events (no story-linked events that day), the day renders as a
bare date heading plus the collapsed "via TMDB" toggle — **visually empty** until the user
clicks. For many admitted films whose only activity is catalog-sourced (credits attaching,
release-date moves, status transitions — the entire reason ADR-0014 introduced
catalog-sourced events), this is most days, and the empty day reads as a broken or
abandoned page.

The collapse originally served two jobs:

1. **Hide TMDB oscillation noise** — credit add/remove/add/remove chains cluttering the
   timeline. **Dampened by NEU-1205** (forward-dwell gate, shipped): a flap is now
   suppressed at carding, so the four-card chain a flap produced no longer reaches the
   frontend. This is the stated precondition for this ticket.
2. **Demote TMDB below trade news for veracity** (NEU-1201's rationale): TMDB is
   community-edited, so its events should not receive equal prominence on a film page.

NEU-1205 addressed only (1). This ticket retires the collapse on the film page anyway: the
oscillation it was hiding is dampened at the source, and the veracity gap (2) is now
signalled by the **"via TMDB" label** (source attribution — kept, Q3/A1) rather than by
**hiding** the events (retired, Q2/A). The feed retains the collapse (Q1).

## Scope

- **Frontend only.** No backend change — the film detail API already emits `day_groups`
  with `news_events` and `tmdb_events` (NEU-1201). The subgrouping axis (`_has_story()`,
  the `EXISTS(event_story)` predicate) is unchanged.
- **Film page only.** The feed (`/feed`) `SectionWrapper` collapsed "via TMDB" pattern is
  unchanged (Q1). The feed is a publication log keyed on `created_at` (ADR-0016), a
  different surface with different day semantics; a feed day aggregates many films and
  shows "via TMDB (N movies)" so it is not visually empty.
- **Removal of the collapse, retention of the label.** The `TmdbSubSection` toggle is
  removed; the "via TMDB" `<h4>` heading is kept as a static, non-collapsible label (Q3/A1).

## Acceptance criteria

- The film page renders `tmdb_events` for a day **inline, with no collapse**, under a
  static "via TMDB" heading, on initial render with no user interaction.
- A day with **only** TMDB events renders: date heading + "via TMDB" heading + the event
  list. No visually-empty day, no collapsed toggle. (Resolves the reported problem.)
- A day with **only** news events renders unchanged from today: date heading + "In the
  news" heading + the event list.
- A day with **both** subgroups renders both labelled sections, both visible: "In the
  news" (news events) then "via TMDB" (TMDB events), separated by the existing
  `SECTION_BREAK` rule. Order within each subgroup is unchanged (`occurred_at ASC,
  created_at ASC, id ASC`, per NEU-1204).
- A day with **no** events is not emitted (unchanged — the backend already omits empty
  days; the frontend `dayGroups.length === 0` empty-state path is untouched).
- The "Latest updates" section heading count (`totalEvents`) is unchanged — it already
  sums `news_events.length + tmdb_events.length` per day group.
- The `EventCard` render for each event is unchanged (`EventOut` shape, `provenance`,
  `sources`, `summary`, confidence) — this is a layout change, not a per-card change.
- The toggle button, the `useState` hook, the chevron SVG, and the `{open && ...}`
  conditional are removed from `TmdbSubSection` (no longer a disclosure). The component
  may be simplified/inlined; the "via TMDB" `<h4>` must remain.
- The feed's `SectionWrapper` ("via TMDB (N movies)", collapsed by default) is **not**
  modified. Feed tests are unchanged.
- `task test && task lint && task typecheck` pass in the frontend repo; changed/new files
  are Prettier-formatted.

### Non-goals / out of scope

- **The feed.** No change to `feed.tsx` `SectionWrapper` or its tests.
- **Backend.** No `day_groups` shape change, no API versioning.
- **Per-card rendering.** `EventCard`, `SourceLinks`, confidence, provenance, sources —
  unchanged. The "via TMDB" attribution inside a card (empty sources) is a separate
  concern from the section heading and is not touched.
- **Fixing the NEU-1201 "unlabelled single subgroup" deviation.** NEU-1201's spec said a
  day with only one subgroup should render an **unlabelled** list; the shipped
  `EventTimeline` always renders the "In the news" heading on a news-only day
  (`EventTimeline.tsx:66-73` — the `<h4>` renders whenever `hasNews`, regardless of
  `hasTmdb`). This is a pre-existing deviation, not introduced by NEU-1207, and this
  ticket does not fix it: under A1 both single-subgroup cases (news-only, TMDB-only)
  render a label, symmetric with each other. Correcting the deviation (dropping the lone
  heading when only one subgroup is present) is a separate ticket if desired.
- **Removal-card publication timing.** NEU-1205's N-day hold is a backend carding rule;
  this ticket does not change it. (See "NEU-1205 interaction" below for the documented
  consequence.)

## Technical decisions

### 1. Retire the hide, keep the signal (Q2/A + Q3/A1)

The collapse was the **hide**; the "via TMDB" label is the **signal** (source
attribution, the other half of ADR-0014's presentation mitigation). NEU-1207 retires the
hide (because NEU-1205 dampened the oscillation it was hiding) but keeps the signal (the
veracity gap remains). So the `TmdbSubSection` becomes a static labelled section,
mirroring how the news subgroup already renders: a `<h4>` heading + `<EventList>`, no
disclosure. The veracity demotion on the film page is now by **label**, not by
**visibility**.

### 2. Film page only, feed unchanged (Q1)

The feed and film page are different surfaces: the feed is a publication log keyed on
`created_at` (ADR-0016) and aggregates many films per day with a "(N movies)" count, so a
TMDB-only feed day is not visually empty; the film page is an event log keyed on
`occurred_at` (NEU-1204) where a TMDB-only day is one film's one day. The reported problem
is film-page-specific. The feed's `SectionWrapper` stays collapsed by default.

### 3. NEU-1205 interaction — transient-invariant hold now visible on the film page

NEU-1205's forward-dwell gate holds a removal card for ≤N days (`SWEEP_CREDIT_DWELL_DAYS`,
default 3) before carding, so during the hold the latest *carded* event for a person is
still the attachment while TMDB says "removed." NEU-1205's spec argued this transient
invariant violation was "confined to the collapsed `rumored` section — invisible on the
feed's expanded section and on the film page's news section." Under NEU-1207 the film
page's TMDB section is **no longer collapsed**, so the ≤N-day mismatch is **visible by
default on the film page** during the hold. This is **accepted as the documented cost of
uncollapsing**:

- **Bounded** (≤N days, N=3 by default) and **self-correcting** (a final departure cards
  once the hold passes; a flap's removal is suppressed).
- The mismatch is **only ever an attachment card staying visible** while a removal is
  pending — it is never a *wrong* attachment (the carded attachment was real); the
  correction card arrives within N days. A user seeing the attachment during the hold
  sees a true-at-the-time beat that TMDB has since retracted.
- The *feed* still confines the mismatch (Q1 — feed TMDB stays collapsed), so the
  high-traffic surface is unaffected; only the per-film page shows it.

This narrows NEU-1205's "confined to the collapsed section" argument to **the feed only**;
recorded in the ADR-0014 amendment (decision 5).

### 4. No `useState` in the new section

The component currently calls `useState(false)` unconditionally (a lint rule requires
hooks before any early return). Removing the toggle removes the only state in
`TmdbSubSection`; the section becomes a pure render of heading + list. If the component is
inlined into `EventTimeline`'s per-group loop, no `useState` is introduced there either
(the outer `CollapsibleSection` is a native `<details>`, not React state). Keep the
component structure if it aids readability; do not add state to gate TMDB visibility.

### 5. ADR-0014 amendment + CONTEXT.md update (Q4)

Retiring the film-page collapse reverses a deliberate NEU-1201 decision (veracity
demotion by hiding), is surprising without context, and is a real trade-off (oscillation
dampened by NEU-1205, veracity gap remains and is now label-only) — meets all three ADR
bars.

- **CONTEXT.md "Day-grouped events"** currently states: *"The TMDB section is collapsed by
  default on the film page the same way it is on the feed."* This becomes false for the
  film page. Amend to split the two surfaces: the **film page** renders the TMDB section
  **inline** (NEU-1207); the **feed** still collapses it. The label ("via TMDB") remains
  the source-attribution signal on both.
- **ADR-0014** gets a dated NEU-1207 amendment recording: the film-page collapse was
  retired because NEU-1205 dampened the oscillation it was hiding; the feed retains it;
  the veracity gap (TMDB community-edited) is now signalled by the label alone on the film
  page, not by hiding; the NEU-1205 transient-invariant "confined to the collapsed
  section" argument now holds for the **feed** only — on the film page the ≤N-day
  mismatch is visible-by-default, accepted as the cost of uncollapsing (bounded,
  self-correcting, feed-confined for the high-traffic surface).

## Implementation plan

### `src/components/film/EventTimeline.tsx`

1. Remove the `useState` import if it becomes unused after step 2 (it is the only `useState`
   in the file — `CollapsibleSection` is a native `<details>`).
2. Replace `TmdbSubSection` with a static render: remove the `<button>`, the chevron SVG,
   the `useState(false)`, and the `{open && ...}` conditional. Keep the `SECTION_BREAK`
   wrapper and the "via TMDB" `<h4>` heading (same classes as the news subgroup's `<h4>`),
   then render `<EventList events={events} />` unconditionally.
3. `EventTimeline`'s per-group loop (`hasTmdb && <TmdbSubSection .../>`) is unchanged in
   structure; only the child component lost its toggle. The news-subgroup conditional
   (`hasNews && ...`) is unchanged.

### Tests — `src/components/film/EventTimeline.test.tsx`

Update/add (the existing test file is the source of truth for behavior):

- **Update** `renders news and tmdb sections when both present` — the TMDB events should
  now be **visible on initial render** without a click (assert the TMDB event summary
  text is present, not just the "via TMDB" label).
- **Add** `renders tmdb events inline without collapse on a tmdb-only day` — a day group
  with only `tmdb_events`: assert the event summary is present on initial render and the
  "via TMDB" heading is present; assert there is **no** toggle button (query by the
  toggle's accessible role/text and expect it absent).
- **Add** `renders both subgroups visible on initial render` — mixed day: both event
  summaries visible immediately, both headings present.
- **Keep** `renders section label even when only one subgroup is present` (the pre-existing
  news-only-day test) — unchanged; it documents the shipped behavior (label always
  renders), which this ticket does not alter.
- **Keep** the empty-state, day-ordering, and within-day-ordering tests unchanged.

### Docs

1. Amend `CONTEXT.md` "Day-grouped events" entry: split film-page (inline, NEU-1207) from
   feed (collapsed); note the label remains the veracity signal on both.
2. Add a dated amendment to `docs/adr/0014-catalog-sourced-events.md` (NEU-1207): retire
   film-page collapse, feed retains it, label-only demotion on the film page, and the
   NEU-1205 transient-invariant argument now scoped to the feed.

## Out-of-scope reminders

- Feed change, backend/API change, per-card rendering, the NEU-1201 unlabelled-subgroup
  deviation, and NEU-1205's carding rule are all unchanged.
