# NEU-1206 — Only show earliest release date per category per country

**Linear:** [NEU-1206](https://linear.app/neuroticsasquatch/issue/NEU-1206/only-show-earliest-release-date-per-category-per-country)
**Spec dir:** `docs/specs/`
**Amends:** ADR-0014 (release-date half) — see `docs/adr/0014-catalog-sourced-events.md`, amendment dated 2026-08-29 (NEU-1206)

## What to build and why

A film can carry **multiple `catalog.film_release_date` rows for the same `(iso_3166_1, release_type)` subject** — TMDB has no uniqueness constraint there, and `_rebuild_release_dates` (`ingest/tmdb/upsert.py:258`) inserts every entry it gets with no dedup. Today this surfaces two defects:

1. **Display duplication.** The movie page (`public/service.py:387-436`) lists every displayable row, so a film with two US-wide dates shows two lines for the same category. The calendar (`public/service.py:857-938`) can list the same film twice on two different days under the same category.
2. **Carding noise / silence.** `diff_release_dates` (`ingest/tmdb/release_date_history.py:135`) keys the subject as `(iso, release_type)` through a dict, so duplicate rows **already collapse last-wins** — a latent bug. The carding rule also has no concept of "the earliest", so it cannot express the ticket's requirement: a card should fire only when a *changed* release date **is now the earliest** for that subject.

This ticket makes **the earliest date per `(country, category)`** the single value that display and carding both track. We name that value the **governing release date** (see `CONTEXT.md`).

## Acceptance criteria

### Movie page (`public/service.py:get_film_detail`)
- For each subject `(iso_3166_1, release_type)` in the displayable set (`catalog.release_grade.is_displayable_release`: US or origin country, theatrical types 2/3), the page shows **exactly one** line: the row with `min(release_date)` for that subject.
- The displayed row's metadata (`certification`, and any other `ReleaseDateOut` fields) comes from that earliest row. Ties on the same earliest date break deterministically by `FilmReleaseDate.id` (stable).
- **No change to `ReleaseDateOut`** — no new fields, `note`/`iso_639_1` stay off the DTO.
- The existing primary-`release_date` fallback (the `if not release_dates and film.release_date is not None` branch at `public/service.py:427`) is unchanged: it fires only when no displayable dates remain at all.
- The earliest date is shown **even when it is in the past.** The movie page is an event log, not an upcoming-only surface; a film that has already opened keeps its earliest release date listed.

### Calendar (`public/service.py:get_calendar`)
- For each `(US, type)` category with `type ∈ {2, 3}`, collapse to the **governing date** (`min(release_date)` over current `film_release_date` rows for that subject) **before** applying the existing upcoming filter (`release_date >= today`).
- A category appears on the calendar iff its **governing date is upcoming**. If the governing date is past, the whole category is excluded — it does **not** fall through to a later upcoming date for that category. (Different categories are independent: a past US-limited + upcoming US-wide still shows the wide one.)
- Pagination, ordering, and all other existing calendar filters (`Film.slug`, `adult`, `runtime`, `popularity`) are unchanged.

### Carding (`ingest/tmdb/release_date_history.py`, `ingest/sweep/release_events.py`)
- A release-date event cards **iff the governing date for a subject changed and the new governing date was not already present in the previous set** for that subject.
- Formal rule (per subject present in the current set, with `previous` the stored set or `None` for a never-observed film):
  - `new_earliest = min(current_set)`, `prev_earliest = min(previous_set)` (`None` if the subject was absent from the previous observation).
  - **Card fires iff** `new_earliest ≠ prev_earliest` **AND** `new_earliest ∉ previous_set`.
  - `new_date = new_earliest`; `previous_date = prev_earliest` (or `None`); `change = "set"` if the previous set was empty, else `"moved"`.
  - **First-observation baseline** (subject never observed → `previous is None`) suppresses the card entirely — unchanged from NEU-1121 / ADR-0014.
- The card describes **the governing date's movement** (`previous_date = prev_earliest`, `new_date = new_earliest`), **not** the underlying moved row's own previous value. See the case table in "Decisions" below.
- One event per `(film, changed_at)` observation is unchanged (`uq_event_catalog_change`); the deterministic body (`ReleaseDatesChanged`) renders one sentence per subject whose governing date carded.

### Out of scope
- **No backfill.** Existing `film_release_date_change` rows and `release_date` events are grandfathered; no destructive rewrite. If a bounded cleanup is wanted later, it is a follow-up ticket (mirroring `scripts/prune_primary_release_events.py`).
- **No schema migration.** `film_release_date_change` keeps its columns; only the *semantics* of what a `moved`/`set` row's `new_date` means change (it is now the governing date, not necessarily the moved row's own date).
- **No DTO change.** `ReleaseDateOut` is untouched.
- **Grouped feed, sitemap** unchanged (they show events / films, not release rows).
- **Withdrawals stay uncarded.** A date disappearing records no history row (ADR-0014), so when a governing date moves to an *unchanged* sibling because the prior governing date was withdrawn, there is nothing to card — the display silently updates. Consistent with ADR-0014's "disappear = no card" and accepted as the cost of the unified rule (same as the credit-detachment half's transient state, but here permanent and by design).

## Key technical decisions and constraints

### 1. Reading A: a card fires only for a *changed* row whose new date is the governing date
The ticket's "an event card for a changed release date should not be created unless it is now the earliest" is read literally. The competing reading (B: "card whenever the displayed earliest changes") would card citing a date that itself never moved — misleading. Reading A also makes the delayed-past-sibling and withdrawal-to-unchanged-sibling cases **silent** on the card, matching ADR-0014's withdrawal philosophy. This silent-display consequence is accepted.

### 2. "The record" = the current set of `film_release_date` rows after this ingest
"Earliest" is `min(current rows)` per subject — the incoming TMDB data, not the historical union. So a date that was removed from TMDB stops counting toward "earliest" on the next ingest.

### 3. Governing date governs both display and carding
One value, two consumers, must not drift — the same structural reason `catalog.release_grade` exists. The collapse is applied at query time in the two display paths and at diff time in the carding path; they share the definition of "governing date" = `min(release_date)` over current rows for the subject.

### 4. Calendar: earliest-overall, then the upcoming filter
The calendar is upcoming-only and collapses to the governing date first. A category whose governing date is past is excluded entirely (it does not fall through to a later upcoming date) — the literal ticket reading, consistent with the movie-page rule.

### 5. Card body cites the governing date's movement, not the moved row's
When a non-governing row moves earlier and crosses below the existing governing date (case table row 7), the card reports `previous_date = prev_earliest` (the old governing date), `new_date = new_earliest` (the moved row's new date). The moved row's own previous value is an implementation detail the card does not surface; the card describes what the display shows (the governing date's movement). This is the one case where the two readings diverge and the chosen one keeps card and page consistent.

### 6. No backfill, no migration
Forward behavior only. Existing events stay; the forward gate applies from the next sweep.

## Case table (subject = US wide; ✓ = card, ✗ = silent)

| prev | current | new_earliest | card? | `previous_date` | body |
|---|---|---|---|---|---|
| `[1 Dec]` | `[15 Dec]` | 15 Dec | ✓ moved | 1 Dec | "moved from 1 Dec to 15 Dec" |
| `[15 Dec]` | `[1 Dec]` | 1 Dec | ✓ moved | 15 Dec | "moved from 15 Dec to 1 Dec" |
| `[15 Dec]` | `[1 Dec, 15 Dec]` | 1 Dec | ✓ moved | 15 Dec | "moved from 15 Dec to 1 Dec" (earlier date added) |
| `[1 Dec]` | `[1 Dec, 15 Dec]` | 1 Dec | ✗ | — | earliest unchanged; later date added silently |
| `[1 Dec, 15 Dec]` | `[1 Dec, 20 Dec]` | 1 Dec | ✗ | — | delayed-past-sibling; 15→20 silent |
| `[1 Dec, 15 Dec]` | `[15 Dec]` | 15 Dec | ✗ | — | governing date withdrawn to unchanged sibling — silent (Reading A) |
| `[1 Dec, 15 Dec]` | `[1 Dec, 10 Dec]` | 10 Dec | ✓ moved | **1 Dec** | "moved from 1 Dec to 10 Dec" (15→10 crossed below 1 Dec; card cites governing, not moved row) |
| `[]` (observed-empty) | `[1 Dec, 15 Dec]` | 1 Dec | ✓ set | `None` | "set to 1 Dec" |
| — (unobserved) | `[1 Dec]` | 1 Dec | ✗ | — | first-observation baseline |

## Known unknowns / deferred

- **No backfill.** A bounded cleanup of any pre-NEU-1206 cards that the new gate would have suppressed is a follow-up ticket, not this one.
- **`note` on the DTO.** The earliest row's `note` is not surfaced (`ReleaseDateOut` has no `note` field). If a future ticket wants festival/preview notes from the governing row, it adds the field then; this ticket keeps the DTO frozen.
- **Withdrawal-to-unchanged-sibling silence** is accepted as the cost of Reading A + ADR-0014's no-history-for-disappearances rule. The display silently moves to the sibling; no card. This is the same philosophy the credit-detachment transient hold accepts, but here it is permanent and by design.
