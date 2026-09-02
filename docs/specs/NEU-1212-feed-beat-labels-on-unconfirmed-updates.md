# NEU-1212 — Beat labels on the feed's "unconfirmed updates" rows

## Problem

NEU-1208 demoted the grouped feed's catalog section to **title-only links**: the backend stopped
shipping `events` for `news_backed=false` rows, and `FeedDayCard`'s existing
`item.events.length > 0` guard then rendered nothing beneath the title. The demotion of the *event
cards* was right — full summaries plus badges gave early TMDB activity the same weight as
trade-sourced news. But removing **all** per-row signal overshot: a reader looking at the expanded
section sees ~35 bare film titles and cannot tell a release-date move from a credit removal without
a page load per film. The row became untriageable.

The fix was already in the payload. `FeedDayItem.event_types` — "every distinct beat this film-day
carries, most-significant first" — is computed, ordered, and shipped for every row, and NEU-1208
deliberately kept it accurate for catalog rows ("accurate, unused in the UI"). Its own DTO comment
describes the intended rendering, and NEU-1140 (`eb67534`, "label the day's beats") shipped exactly
that before commit `e39da9d` replaced beat badges with full event cards. Nothing renders it today.

**This ticket restores the beat labels, not the events.** It is not a reversal of NEU-1208: no
backend code changes, catalog rows still ship `events=[]`, the section stays collapsed by default,
and both the "unconfirmed updates" rename and the removed per-card "via TMDB" attribution stand.

### Measured volume (production, 30 days, catalog-only film-days)

| Distinct beat types in a film-day | Film-days |
|---|---|
| 1 | 552 |
| 2 | 66 |
| 3 | 15 |
| 4 | 1 |

Same-type repeats within a day occur on **4 of 634** film-days. So a row renders one badge ~87% of
the time and never more than four. Catalog activity runs ~22–64 events/day across ~21–52 films.

## Scope

- **Frontend rendering + docs.** `FeedDayCard` renders `event_types` as badges on rows that carry
  no event cards. Backend gets **docs and comment corrections only** — no code, no DTO, no
  migration, no new tests.
- **Feed only.** The film page renders full events inline (NEU-1207) and is untouched. Calendar,
  sitemap, and film index are untouched.
- **Two repos, two PRs**, both `(NEU-1212)`, branch
  `tom/neu-1212-add-beats-back-to-unconfirmed-updates-movies-in-front-page`.

## Acceptance criteria

### Frontend — `src/components/feed/FeedDayCard.tsx`

- **A row with `events: []` renders one badge per entry in `item.event_types`**, in the order the
  backend shipped them (most-significant first — do not re-sort).
- **Badges render inside the title `<Link>`**, immediately after the year/arc-stage parenthetical,
  on the same line. They are `<span>`s, so the nested-anchor problem that forced `ba18db9` to pull
  the source chips out of the link does not apply.
- **Badge styling matches the news-card event badge** (`FeedDayCard.tsx:37-39`) exactly, except
  `ml-1` in place of `mr-1`: `ml-1 inline-block rounded bg-muted px-1.5 py-0.5 align-middle
  text-[10px] font-medium uppercase leading-none tracking-wide text-muted-foreground`. Label text
  comes from `eventTypeLabel`.
- **The render predicate is `item.events.length === 0 && item.event_types.length > 0`** — a row
  shows either its beats or its event cards, never both.
- **A row with events (news-backed) renders no title-level badges** — unchanged full event cards,
  each with its own per-event badge.
- **No count renders** — not `event_count`, not a per-type multiplier. The section heading keeps
  its `(N movies)` suffix.
- **Long titles still wrap with the hanging indent** (`-indent-3 pl-3`); badges flow after the last
  word of the wrapped title.

### Frontend — `src/routes/feed.tsx`

- **No change.** The "unconfirmed updates" section stays collapsed by default
  (`useState(section.key !== "tmdb")`), keeps its `(N movies)` suffix, and the both-sections-always
  / "None today" structure from NEU-1208 is untouched.

### Backend — no code change

- `_make_item(..., ship_events=False)` for catalog rows (`service.py:782`) is **unchanged**.
  `event_types`, `top_event_type`, and `event_count` continue to be computed from the real
  `catalog_events` list. `top_event_type` remains unrendered.

### Docs — ADR-0014, CONTEXT.md, comments

- **ADR-0014 gets a dated NEU-1212 amendment** stating: NEU-1208's demotion overshot at the *row*
  level — removing the event cards was right, removing all signal left a row that cannot be
  triaged without a page load; and the remedy cost nothing because `event_types` was specified for
  this rendering and NEU-1208 kept it accurate. It must state plainly what did **not** change:
  `events` is still not shipped for `news_backed=false` rows, the section is still collapsed by
  default, the "unconfirmed updates" label and the removed per-card attribution both stand — so
  NEU-1205's "confined to the collapsed section" transient-invariant argument survives intact.
- **CONTEXT.md line 474** ("...which now renders title-only links (NEU-1208)") and **line 498**
  ("...demoted to **title-only links** under NEU-1208: ...a reader sees only the film title and
  must click through to view the updates") are corrected: the reader now sees the film title plus
  a badge for each distinct beat of that film-day, and clicks through for the summaries.
- **CONTEXT.md gains a `**Beat**` entry** beside `**Event**` (glossary, "Story lifecycle" section):
  a beat is the real-world development itself; an **Event** is the record of one. Note that "the
  day's beats" on the feed means the distinct `event_types` of a film-day, and that this is the
  term `**Split beat**`, `**Over-merge**`, and `eventTypeLabel`'s doc comment already lean on.
- **Stale comments corrected**: `dto.py:130-133` and `types.ts:148-150` both say the feed labels
  the set *"beneath the title"* — it now renders inline after the title. `FeedDayCard`'s doc
  comment (`FeedDayCard.tsx:5-8`) still describes every row as "each event as a summary line with
  real anchor source chips below it", which has been only half-true since NEU-1208; it should
  describe both row shapes.

### Tooling

- Frontend: `task test`, `task lint`, `task typecheck` green; changed files Prettier-formatted.
- Backend: `task test`, `task lint`, `task typecheck` green (docs/comments only, but CI runs
  `ruff format --check`).

## Technical decisions

### 1. Every beat type, not just `top_event_type` (Q-R2.1)

`event_types` is the distinct set; `top_event_type` is its head. They differ on ~13% of film-days.
Rendering only the head would show a day pairing a release-date move with a casting beat as
"Release date", silently swallowing the other — the exact failure the DTO comment was written to
prevent. The cost is bounded: 3+ badges occur on 16 of 634 film-days.

### 2. No counts (Q-R2.2)

Same-type repeats occur on 4 film-days in 30 days, so a `×N` suffix would render essentially never
— and computing it per-type would need `events`, which catalog rows do not ship. A row total
(`event_count`) is available but puts a number next to content that isn't there, inviting a click
it cannot reward. The heading's `(N movies)` already carries the day's magnitude.

### 3. The collapse stays (Q-R2.3)

The section remains collapsed by default, so the default front-page view is unchanged and the beat
labels pay off on expansion — one click yields ~35 films each tagged with what changed. Keeping it
also preserves the load-bearing argument in the ADR chain: NEU-1205's accepted transient-invariant
violation is justified as "confined to the collapsed section", and NEU-1207 explicitly retained the
feed's collapse when retiring the film page's. Un-collapsing would re-open both and is not what
this ticket asks for.

### 4. Inline news-card pill, inside the title link (Q-R3.1, Q-R4.1)

NEU-1140 put beat badges on their own wrapped line beneath the title at `text-[11px]`; the news
card's event badge is inline at `text-[10px]`. We take the **news-card pill**, so one badge style
means "beat" sitewide and no second, dimmer variant is introduced whose only job is to repeat what
the section heading directly above already says.

Placement forces the structure. The title `<Link>` is `display: block`, so badges added as siblings
*after* it land on their own line — the beneath-the-title layout we are not taking. Putting them
**inside** the `<Link>`, after the year span, is one line, inherits the `-indent-3 pl-3` hanging
indent for free, and preserves the wrap behaviour that `"wraps a long title instead of truncating
it"` pins. `ba18db9`'s rule — keep things out of the card link — was specifically about `<a>`
source chips: nested anchors are unclickable and invalid HTML. `<span>` badges have no such
problem, and NEU-1140 had them inside the link deliberately.

**The badges join the link's accessible name** — the row announces as *"Sinners (2025) Casting
Release date, link"*. Accepted, not hidden: the badges are content, not decoration. "Casting" is
the only information the row carries beyond the title, and `aria-hidden` would leave screen-reader
users with exactly the untriageable title-only row this ticket exists to fix.

### 5. Predicate on `events.length`, not `news_backed` (Q-R3.2)

`item.events.length === 0` states the real invariant — a row shows its beats or its events, never
both — in terms of what the component can observe, and keeps `FeedDayCard` ignorant of the
sectioning rule, which is `feed.tsx`'s job. `!item.news_backed` would re-derive the backend's
`ship_events` decision in the frontend and would double-render if the backend ever shipped events
for catalog rows again.

### 6. ADR amendment + `**Beat**` glossary entry (Q-R3.3, Q-R4.2)

ADR-0014's NEU-1208 amendment asserts a "title-only links" contract that a reader would find
contradicted on screen — the ADR would be actively wrong, which this repo does not tolerate. The
trade-off (partial re-promotion of TMDB activity four days after deliberately demoting it) is the
kind each of the NEU-1205/1207/1208 amendments recorded.

The glossary defines **Event** and **Split beat**, the latter using "beat" as a primitive it never
defines, while the codebase uses the word three ways: the development, the `event_type` classifying
it, and now the rendered badge. This ticket promotes "beat" to a UI concept, which makes the gap
load-bearing; the glossary's stated job is that its terms are enforced in code.

## Implementation plan

### Frontend — `src/components/feed/FeedDayCard.tsx`

1. Inside the title `<Link>`, after the year/arc-stage `<span>`, add a guarded fragment:
   `{item.events.length === 0 && item.event_types.length > 0 && (...)}` mapping `item.event_types`
   to `<span key={eventType}>` badges with the class string in the acceptance criteria and
   `eventTypeLabel(eventType)` as the text.
2. Leave the existing `{item.events.length > 0 && (...)}` event-list block untouched.
3. Update the component doc comment to describe both row shapes: a news row renders event summary
   lines with source chips; a catalog row renders a badge per beat inline after the title.

### Frontend — `src/api/types.ts`

1. Correct the `event_types` comment (lines 148-150): the feed labels the whole set **inline after**
   the title, not beneath it.

### Backend — docs only

1. `docs/adr/0014-catalog-sourced-events.md`: append the dated **NEU-1212** amendment described in
   the acceptance criteria, after the NEU-1208 one, ending with the pointer
   `docs/specs/NEU-1212-feed-beat-labels-on-unconfirmed-updates.md`.
2. `CONTEXT.md` lines 474 and 498: correct the title-only claims.
3. `CONTEXT.md`: add the `**Beat**` entry beside `**Event**` in the "Story lifecycle" section,
   with an `_Avoid_:` line consistent with neighbouring entries.
4. `src/upmovies/public/dto.py` lines 130-133: correct "beneath the title" to the inline rendering.

## Tests

**Both existing titles-only tests pass unchanged after this change while asserting the opposite of
the new intent** — they are green-but-lying and must be rewritten, not relied on. `feed.test.tsx:418`
asserts `queryByText("Trailer")` is null but its fixture's `event_types` is `["casting"]`, so the
new badge reads "Casting" and the assertion still holds. `FeedDayCard.test.tsx:117` has the same
shape (fixture `event_types: ["release_date"]`, asserts "Trailer"/"Casting" absent).

### `src/components/feed/FeedDayCard.test.tsx`

- **Rewrite** `renders title only when events are empty` → `renders beat labels when events are
  empty`: with `events: []` and `event_types: ["release_date"]`, assert the "Release date" badge
  **is present** and no event summary text renders.
- **Add** `renders a badge for every beat type` — `events: []`,
  `event_types: ["release_date", "casting"]` renders both badges, in that order.
- **Add** `renders no beat labels on a row with events` — a news-backed row with a populated
  `events` array renders its event cards and no title-level badge set (assert the event-type label
  appears exactly once).
- **Add** `beat labels render inside the title link` — the badge text is within the
  `getByRole("link")` subtree.
- **Keep** the zebra-stripe, long-title-wrap, and existing event-card tests unchanged.

### `src/routes/feed.test.tsx`

- **Rewrite** `renders movie titles only without event cards` → assert that, after expanding
  "unconfirmed updates (1 movie)", the row shows the film title **and** its beat badge, and does
  **not** show the event summary text.
- **Keep** the collapse-by-default, both-labels, "None today", within-day ordering, poster-strip,
  and "view more" tests unchanged.

### Backend

- **No new or changed tests** — no backend code change.

## Out of scope / deferred

- **Un-collapsing the feed's "unconfirmed updates" section** — explicitly not done; the collapse is
  load-bearing for NEU-1205's transient-invariant argument.
- **Shipping `events` for `news_backed=false` rows** — NEU-1208's backend behaviour stands.
- **Restoring the per-card "via TMDB" attribution** — stays removed (NEU-1208); the section heading
  remains the sole veracity signal.
- **The "unconfirmed updates" label and the both-sections-always / "None today" structure** —
  unchanged.
- **The film page** — already renders events inline (NEU-1207); no beat labels added there.
- **Rendering `top_event_type` or `event_count`** — both stay in the payload, unrendered.
- **Restyling the news-card event badge** (the `text-[10px]` / NEU-1140 `text-[11px]` split is
  resolved by adopting the news-card pill; no separate restyle).
- **Calendar, sitemap, film index** — untouched.
