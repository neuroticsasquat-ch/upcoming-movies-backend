# NEU-1215 — Changes to the movie title parenthetical

## Problem

The parenthetical after a film title renders `release_year ?? arcStageLabel(arc_stage)` on three of
the four surfaces that show it (`FilmHeader.tsx:18`, `FeedDayCard.tsx:24`,
`SearchResultItem.tsx:32`; the calendar row shows the year alone). Two things are wrong with it.

**The arc-stage fallback misleads.** `(Announced)` in the year's slot reads as though it were a
year — a fact about the film's release — when it is really a restatement of TMDB's `status` column.
The fallback was a deliberate choice, documented at `FilmHeader.tsx:8`: *"with thousands of undated
films, an empty slot reads as missing data instead of a meaningful state."* That reasoning was
sound when the year was the parenthetical's only content. It stops being sound once the
parenthetical can carry country and director, because the slot then fills for almost every film on
its own merits.

**The parenthetical is thin.** A feed row is a bare title plus a year, which for this catalog is
mostly a bare title plus nothing: **78% of films (7,409 of 9,550) have no release date at all**.
Country and director are the two facts that let a reader place an unfamiliar title without a page
load, and both are already in the catalog — `catalog.film_production_country` is rebuilt every
ingest (`ingest/tmdb/upsert.py:241`) and director credits already power the calendar's
`_calendar_directors()` (`public/service.py:786`). Neither reaches the feed's read model.

### Measured coverage (local DB, refreshed from production — 9,550 films)

| | Films | Share |
|---|---|---|
| No release year | 7,409 | 78% |
| No production country | 1,853 | 19% |
| No director credit | 850 | 9% |
| **None of the three** | **201** | **2.1%** |

Element-count tails, which drive the capping rules below:

| Values | 0 | 1 | 2 | 3 | 4 | 5 | 6+ |
|---|---|---|---|---|---|---|---|
| Production countries | 1,853 | 6,453 | 896 | 243 | 59 | 23 | 23 |
| Director credits | 850 | 8,191 | 454 | 27 | 9 | 3 | 15 |

The worst real case is *Jenjira's Magnificent Dream* — nine production countries
(`CA/CH/CO/FR/GB/MX/NL/TH/US`) — and one film carries **14** director credits. 45 films exceed three
countries; 43 exceed two directors.

## Decision

Two surfaces change, and they change **differently** — the film page and the feed have different
affordances around the title, so forcing one parenthetical shape onto both would duplicate content
on one of them.

**Feed row** gets the full parenthetical from the ticket:

```
Inception (USA, Dir: Christopher Nolan, 2010)
The Favourite (Ireland/UK/USA, Dir: Yorgos Lanthimos, 2018)
Parasite (South Korea, Dir: Bong Joon-ho, 2019)
Drive My Car (Japan, Dir: Ryusuke Hamaguchi)          ← no year; no fallback
No Country for Old Men (USA, Dir: Ethan Coen/Joel Coen, 2007)
Jenjira's Magnificent Dream (Canada/Colombia/France +6, Dir: Apichatpong Weerasethakul)
Untitled Fifth Project (Announced)                     ← last resort only
```

**Film page** does *not* get it. Its spec sheet (`FilmHeader`'s `<dl>`) is already the structured
record and already lists Director beside Screenplay/Writer/Story; repeating the director 100px above
its own labelled row is duplication, not emphasis. Instead the film page **keeps its year-only
parenthetical** (minus the arc-stage fallback) and **gains a `Countries` row** in the spec sheet —
the one element of the parenthetical the sheet was missing.

The year stays in the film page `<h1>` rather than moving into the spec sheet because the
`Release dates` section cannot stand in for it: `ReleaseDates.tsx` returns `null` when empty, and
its input is the *displayable* cut (US-or-origin, theatrical types 2/3) defined in
`catalog/release_grade.py`, which by design excludes TMDB's primary `film.release_date`. **845 of
the 2,141 dated films (39%) have no displayable release rows**, so stripping the h1 parenthetical
would erase the year from those pages entirely.

## Scope

- **Backend + frontend, two repos, two PRs**, both `(NEU-1215)`, branch
  `tom/neu-1215-changes-to-movie-title-parenthetical`.
- **Backend:** new read-model fields on `FilmDetailResponse` and `FeedDayItem`, a curated country
  abbreviation map, and a generalised director lookup. **No migration** — every fact needed is
  already stored and populated.
- **Frontend:** `FeedDayCard` and `FilmHeader` only, plus a shared formatting helper.
- **Search dropdown and calendar row are untouched.** `SearchResultItem` keeps its arc-stage
  fallback and `CalendarFilmRow` keeps its year-only parenthetical and its separate `Dir.` line.
  This is a deliberate scope cut, not an oversight — see *Out of scope*.

## Acceptance criteria

### Backend — read models (`public/dto.py`)

- **`FeedDayItem` gains `production_countries: list[str]` and `directors: list[str]`.** Both default
  to `[]`, never `None`. Countries hold **display forms** (already abbreviated), sorted by display
  name ascending. Directors hold person names ordered by billing (`credit_order` asc, nulls last,
  then name asc) — the same order `_calendar_directors` uses today.
- **`FilmDetailResponse` gains `production_countries: list[str]`**, same contract. It does **not**
  gain `directors` — the film page reads directors from the `crew` it already ships, as
  `FilmHeader`'s `billing` does today.
- **`FeedDayItem.arc_stage` stays.** The feed still needs it for the last-resort case. Its DTO
  comment must be updated: it is no longer "rendered in place of the year for an undated film" but
  "rendered only when the film has no country, director, or year".
- **`FilmIndexItem` and `CalendarItem` are unchanged.**
- **Neither list is capped server-side.** Capping is presentation and differs per surface.

### Backend — country display map (`public/`)

- **A curated `ISO 3166-1 alpha-2 → display name` map**, alongside the existing display-label
  constants in `public/release.py` (which establish the convention that presentation labels live in
  `public/` while membership rules live in `catalog/`).
- **Unmapped codes fall through to `catalog.production_country.name`**, which is stored and
  populated for every code in use. The map therefore only carries codes whose TMDB name is wrong for
  a title parenthetical. Of the 118 distinct countries in the catalog, only 11 have names over 14
  characters, so the seed map is small:

  | Code | TMDB name | Display |
  |---|---|---|
  | `US` | United States of America | USA |
  | `GB` | United Kingdom | UK |
  | `AE` | United Arab Emirates | UAE |
  | `BA` | Bosnia and Herzegovina | Bosnia |
  | `SY` | Syrian Arab Republic | Syria |
  | `KG` | Kyrgyz Republic | Kyrgyzstan |
  | `PS` | Palestinian Territory | Palestine |

- **A code with no row in `catalog.production_country`** falls through to the raw code rather than
  being dropped — a missing name is a catalog gap, not a reason to hide a co-production partner.

### Backend — lookups (`public/service.py`)

- **`_calendar_directors` is generalised to `_directors_for_films`** and returns
  `dict[UUID, list[str]]` rather than a pre-joined string. The calendar call site joins with `", "`
  to preserve its current output exactly; the feed passes the list through.
- **A new `_production_countries_for_films(session, film_ids) -> dict[UUID, list[str]]`** joins
  `film_production_country` to `production_country`, maps each code through the display map, and
  sorts by display name. Films with no countries are omitted from the dict (callers default to
  `[]`), matching `_directors_for_films`.
- **Both are batched over the page's film ids**, in the same style as the existing
  `sources_by_event` / `_calendar_stars` lookups — two additional queries per feed page, not per
  row. The feed handler already collects `{r.film_id for r in rows}` (`service.py:707`).
- **Ordering is deterministic.** `film_production_country` is keyed `(film_id, iso_3166_1)` with **no
  ordinal column**, so TMDB's ordering is discarded on write. The sort by display name is what makes
  the rendered list stable across renders and re-ingests. Do not rely on row order.

### Frontend — shared helper (`src/lib/format.ts`)

- **`formatCountryList(countries: string[], opts?: { cap?: number }): string | null`** — joins with
  `/`, returns `null` for an empty list. With `cap` set, renders the first `cap` values followed by
  ` +N` where `N` is the remainder (`"Canada/Colombia/France +6"`). Without `cap`, renders all.
- **`filmParenthetical(input): string | null`** — composes the feed's string from
  `{ production_countries, directors, release_year, arc_stage }`:
  1. Countries via `formatCountryList(..., { cap: 3 })`, if any.
  2. `Dir: ` + directors joined with `/`, capped at 2 with ` +N`, if any.
  3. `release_year`, if non-null.
  - Present elements joined with `", "`. Absent elements contribute nothing — **no empty slots, no
    placeholder separators**.
  - When all three are absent, return `arcStageLabel(arc_stage)`.
  - The function returns the string **without** surrounding parens; the caller supplies them.
- **Co-located unit tests** per the repo convention for `lib/` helpers.

### Frontend — feed row (`FeedDayCard.tsx`)

- **The parenthetical renders `filmParenthetical(item)`**, wrapped in `( )`, in the existing
  `font-normal text-muted-foreground` span at line 24. It is never empty — the arc-stage last resort
  guarantees content.
- **The existing `-indent-3` / `indent-0` interaction with the beat badges (NEU-1214) must survive.**
  The parenthetical stays plain text inside the link, so the badge fix is unaffected — but a longer
  parenthetical makes the hanging indent's wrap behaviour visible on more rows, so check a
  three-country, two-director row wraps cleanly.

### Frontend — film page (`FilmHeader.tsx`)

- **The `<h1>` parenthetical is the release year alone.** `arcStageLabel` is no longer called here
  and its import is dropped.
- **When `release_year` is null the parenthetical is omitted entirely** — no parens, no empty span.
  The `ArcStepper` immediately below already renders production status, so nothing is lost.
- **A `Countries` row is added to the spec sheet `<dl>`, above the `billing` rows**, mirroring the
  ticket's ordering (country first). Label is `Country` for one value, `Countries` for more.
- **The row is uncapped and stacks one country per line**, in the same `names.map(... <div>)` shape
  the `billing` rows already use. The spec sheet is the complete structured record and has the
  width; capping belongs to the feed's constrained line.
- **The row is omitted entirely when the film has no production countries** — consistent with how
  `billing`, `runtime`, `rating`, and `genres` rows already behave.
- **The component docstring at `FilmHeader.tsx:8` must be rewritten.** It currently documents the
  arc-stage fallback as an intentional decision; leaving it in place would leave the file arguing
  against its own behaviour.

### Frontend — meta title (`routes/film.tsx:47`)

- **Unchanged.** `film.release_year ? \`${film.title} (${film.release_year})\` : film.title` already
  matches the new h1 behaviour exactly — year only, no fallback, omitted when absent. Verify, don't
  edit.

## Technical decisions

**Countries join with `/`, elements with `, `.** The parenthetical is a comma-separated list of
elements, so a multi-value element cannot also use commas — `(USA, Dir: Joel Coen, Ethan Coen, 2010)`
is unparseable. One separator convention (`/`) for every multi-value element keeps the comma
meaning "next element" everywhere. Directors use `/` rather than the more idiomatic ` & ` to avoid a
third separator style.

**`production_countries`, not `origin_country`.** The ticket asks for production countries, and the
join table is the fuller list for co-productions. `catalog.film.origin_country` was the cheaper
option — it is an `ARRAY(Text)` on the film row, needs no join, and preserves TMDB's ordering — but
it is a narrower list, so *The Favourite* would render `(UK)` instead of `(Ireland/UK/USA)`. The
cost of the join is one batched query per feed page. Note that the two lists remaining
independent is fine: `origin_country` keeps its existing job in `catalog/release_grade.py` and is
not touched here.

**No ordinal column added.** Preserving TMDB's own country ordering would need a migration, an
ingest change, and a backfill before it read correctly for the 7,700 films already stored. Sorting
by display name is deterministic, needs none of that, and no consumer has asked for TMDB's order.
Revisit only if the arbitrary-looking order of a co-production is actually reported as wrong.

**Caps differ per surface, deliberately.** The feed row is one line in a dense list and caps at 3
countries / 2 directors — covering 99.5% of films untouched while bounding the worst case. The film
page spec sheet is the complete record and caps at nothing. This is the same reasoning that already
gives the calendar and the film page different release-bucket labels (`public/release.py`).

**Backend ships parts; the frontend composes.** A preformatted `title_parenthetical` string from the
backend would be one implementation and one set of tests — but the two surfaces differ in their
empty-case handling and their caps, so the difference would have to be encoded as a backend flag,
pushing per-surface presentation into the service layer. Shipping `list[str]` parts keeps the
surface-specific rules in the one layer that knows about surfaces.

**The abbreviation map lives in the backend, not the frontend.** It sits with the other display
constants in `public/`, so a future server-rendered surface (sitemap titles, OG tags, an email
digest) can reuse it. The frontend receives display-ready strings and never sees an ISO code.

**The arc-stage fallback survives only on the feed.** The film page has an `ArcStepper` directly
below the title showing the same fact in a better form, so `(Announced)` there is pure duplication.
The feed row has no such affordance, and for the 201 films with none of the three elements a bare
title with no parenthetical at all would read as a rendering bug. This is a narrowing of the
NEU-era decision documented at `FilmHeader.tsx:8`, not a wholesale reversal — the fallback still
exists, it just stops being reachable whenever the parenthetical has any real content.

## Out of scope / deferred

- **Search dropdown (`SearchResultItem.tsx`).** Keeps `release_year ?? arcStageLabel(arc_stage)` in
  its cramped right-aligned trailing slot. Adding country and director there would need
  `production_countries`/`directors` on `FilmIndexItem` and a layout rethink for a component whose
  whole job is fast scanning. The arc-stage fallback therefore *survives here* even though the
  ticket's first bullet argues against it — flagged explicitly so it is not read as a missed
  requirement.
- **Calendar row (`CalendarFilmRow.tsx`).** Already renders `Dir. {director}`, stars, and genres on
  their own lines; folding them into a parenthetical would be a redesign of the row, not this
  ticket. Its `director` field keeps its current pre-joined-string contract.
- **No migration, no ingest change, no backfill.** Every fact is already stored and refreshed each
  ingest.
- **`catalog.film.origin_country` is untouched**, as is `catalog/release_grade.py` and everything
  downstream of it (displayable release dates, `_region_visible`, release-date event recording).
- **Country name coverage is not audited.** The map seeds seven entries; anything else falls through
  to the stored TMDB name. If a name later reads badly in a parenthetical, add a map entry — that is
  a one-line change by design, not a follow-up ticket.
- **No new event or beat.** Countries and directors changing does not card. `TRACKED_FIELDS` in
  `ingest/sweep/field_events.py` stays `("status",)`.

## Tests

**Backend**

- `_production_countries_for_films`: multi-country film returns display forms sorted by display
  name; mapped codes (`US`→`USA`, `GB`→`UK`) render abbreviated; an unmapped code renders its stored
  TMDB name; a code with no `production_country` row renders the raw code; a film with no countries
  is absent from the dict.
- `_directors_for_films`: returns a list ordered by `credit_order` nulls-last then name; a film with
  no director credits is absent; the calendar call site still produces its `", "`-joined string
  unchanged (regression — the calendar's rendered output must not move).
- Feed endpoint: `production_countries` and `directors` are populated on `FeedDayItem`, default to
  `[]`, and cost a bounded number of queries regardless of page size.
- Film detail endpoint: `production_countries` populated; `directors` **not** added; `crew`
  unchanged.

**Frontend**

- `formatCountryList`: empty → `null`; one value; three values joined `/`; nine values with
  `cap: 3` → `"A/B/C +6"`; uncapped nine values render all nine.
- `filmParenthetical`, one case per shape: all three present; country+director, no year (the 78%
  case); year only; country only; director only; none of the three → arc-stage label; two directors
  joined `/`; 14 directors → `"X/Y +12"`.
- `FeedDayCard`: renders the composed parenthetical; renders `(Announced)` for a film with none of
  the three; the NEU-1214 badge indent behaviour still holds on a row with a long parenthetical.
- `FilmHeader`: renders `(2010)` for a dated film; renders **no** parenthetical for an undated one
  (assert absence, not an empty string); renders a `Countries` row with one line per country;
  renders `Country` singular for one; omits the row entirely when there are none; still renders the
  `Director` billing row.

## Implementation plan

1. **Backend** — add the country display map beside `public/release.py`'s label constants; add
   `_production_countries_for_films`; generalise `_calendar_directors` → `_directors_for_films` and
   adapt the calendar call site; add the DTO fields and wire them into `_make_item` and the film
   detail builder; update the `arc_stage` DTO comment. Tests, then
   `task format && task test && task lint && task typecheck`.
2. **Frontend** — add `formatCountryList` and `filmParenthetical` to `lib/format.ts` with tests;
   update `FeedDayCard`; update `FilmHeader` (drop the fallback, add the `Countries` row, rewrite the
   docstring); verify `routes/film.tsx:47` needs no change. Then
   `task test && task lint && task typecheck`.

Backend ships first — the frontend fields don't exist until it does.

## Related

- **NEU-1121** — removed `release_date` from `TRACKED_FIELDS`; its rationale note at
  `ingest/sweep/field_events.py:62` says the primary date's "whole surface is now the year
  parenthetical after the title". Still true after this ticket, and still uncarded.
- **NEU-1208 / NEU-1212 / NEU-1214** — the feed row's current shape: catalog rows ship no events,
  carry inline beat badges, and depend on the `-indent-3` / `indent-0` pairing.
- **NEU-1201 / NEU-1207** — the film page's event rendering, untouched here.
- **ADR-0014** (catalog-sourced events) and **ADR-0016** (the feed groups by publication date) —
  neither is contradicted by this change.
