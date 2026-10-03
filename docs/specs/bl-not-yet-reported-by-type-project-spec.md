# bl: Not Yet Reported by Type — project spec

**Status:** approved design, scaffolded in Linear by `/personal:projectit` (2026-10-01)
**Builds on:** ADR-0014 and its amendments (NEU-1200 removal cards, NEU-1205 forward dwell,
NEU-1406 heading, NEU-1467 uncollapsed section), ADR-0016 (the feed groups by publication date),
NEU-1199 (one feed row per film, day and provenance section). Nothing here reopens those
except where §4 says so.
**Repos:** `upcoming-movies-frontend` (the layout), `upcoming-movies-backend` (the removal split)
**Glossary:** frontend `CONTEXT.md` — **Update type** added; **Not yet reported** and
**Demotion** amended. Backend `CONTEXT.md` — **Credit detachment event** rewritten. Use those
terms.
**ADR:** ADR-0014 amendment dated 2026-10-01 (removals split by role class).

This document is the project-wide spec that `/personal:implementit` falls back to for any
ticket in the project (ADR-0004 of the personal plugin). It records the outcome of the
project-shaping interview held with Tom on 2026-10-01 (a `/grilling` pass inside
`/projectit`): every decision, what it changes about what ships today, and the milestone
contracts.

---

## 1. Purpose

The grouped feed ("All updates", `/feed`) and the timeline ("My feed", `/`) lay every day out
as two provenance sections, **In the news** then **Not yet reported**, and each section as one
row per film listing that film's events. In the news reads well that way: a story is about a
film. Not yet reported does not. It is TMDB's change log, so a busy day is dozens of film rows,
each carrying one or two deterministic lines ("X joins the cast.", "US Wide release date
slipped from … to …."). A reader who wants to know "what dates moved today?" or "who got cast?"
has to read every row.

This project lays the Not yet reported section out by **update type**: Now available, Trailer,
Release date, Production status, Cast, Crew, Studios, Franchise. Each heading holds the films
that changed that way that day.

One backend gap blocks a clean grouping. A credit removal is one `credit_removed` card covering
every role, so a single card can say "D.C. Shen is no longer attached to write. Alan Bell and
Jack Black depart the cast." It has no single heading to sit under. The backend splits it by
role class into `cast_removed` and `crew_removed` and migrates the existing cards.

## 2. Scope

**In**

- The Not yet reported section of the grouped feed and the timeline, both rendered by
  `FeedDayGroups` (`frontend/src/components/feed/FeedDayGroups.tsx`).
- The two section headings' movie counts (dropped, NR-6).
- Splitting `credit_removed` into `cast_removed` + `crew_removed`: sweep, vocabulary, data
  migration (NR-9 to NR-12).
- The beat labels for the two new types on both label maps, and aligning the frontend map to the
  digest's wording (NR-13, NR-14).

**Out**

- **In the news.** It keeps its film rows exactly as today, apart from losing its count.
  This is deferred, not rejected; Tom raised it after scaffolding (2026-10-01).
  - Local data, which runs to about 2026-09-01: In the news has a median of 2 films a day and
    a max of 16, and 1% of film-days have more than one change type. Not yet reported has a
    median of 29.5 films a day.
  - At that size, headings would mostly hold one film each.
  - Revisit if In the news regularly reaches about 10 or more films a day, for example after
    more sources or seed methods land.
  - Try a cheaper step first: order In the news by significance. `splitByNewsBacked` currently
    re-sorts by title and discards the backend's significance order.
  - NR-7 keeps the switch cheap.
- **The film page** (`EventTimeline` / `EventCard`). It stays laid out by film-day and section.
  It changes only through the shared label map (NR-13, NR-14).
- **The digest's structure.** It still groups by film entry; only its label map changes.
- **The feed DTO** (`FeedDayItem`, `EventOut`). There is no shape change. Rows keep the NEU-1199
  contract: one row per (film, day, section), each row's `event_types` derived from its own
  `events`.
- Day pagination, the day's poster strip (`FeedDayPosters` / `dayPosterLeads`), the
  `SECTION_SPLIT_EXPLAINER` text and its placement, and the `news_backed` split itself.
- Any truth signal. The **confidence badge** is still the only one.

## 3. Today, in one paragraph

`FeedDayGroups` lays items out as follows:
- It buckets items by `day` (`groupByDay`).
- It splits each day on `news_backed` (`splitByNewsBacked`, which natural-title-sorts the day
  first).
- Each non-empty section gets a static h3 reading `"<label> (N movie[s])"`, followed by one
  `FeedDayCard` per row.

`FeedDayCard` renders the following:
- The linked title plus `filmParenthetical`.
- One `FeedEvent` line per event: beat pill, confidence pill, summary, edited pill, and a
  "later retracted" link when superseded.
- Source chips (none for a catalog event).
- A `JustWatchAttribution` when `event_types` includes `now_available`.

A catalog row arriving with `events: []` falls back to inline beat badges (NEU-1212). Since
NEU-1467 that only happens against an older backend.

## 4. Decisions

### The layout (frontend)

**NR-1 — Only Not yet reported regroups.** It applies on the grouped feed and the timeline, the
two `FeedDayGroups` callers. In the news keeps its `FeedDayCard` rows unchanged. The film page
is untouched (§2).

**NR-2 — Day stays the outer axis.** ADR-0016 stands. A day still renders its poster strip,
then In the news, then Not yet reported. Inside Not yet reported, the order is:
**update-type heading → film row → that film's events of that type.**

```
Oct 1
  [poster strip]
  In the news
    Dune: Part Three (US, Dir: Villeneuve, 2026)
      [Casting] [confirmed] Zendaya returns …
  Not yet reported
    Release date
      Heat 2 (US, Dir: Mann, 2027)
        [confirmed] US Wide release date slipped from Aug 6 to Nov 12.
    Production status
      Blade (US, 2027)
        [confirmed] Shooting has started.
    Cast
      Blade (US, 2027)
        [unconfirmed] Mahershala Ali departs the cast.
      Heat 2 (US, Dir: Mann, 2027)
        [unconfirmed] Adam Driver joins the cast as Chris.
```

**NR-3 — The update types, their members and their order.** The order is fixed and follows the
arc significance ranking (`_EVENT_STAGE` in `backend/src/upmovies/public/arc.py`), most
significant first:

| # | Heading | `event_type`s |
|---|---|---|
| 1 | Now available | `now_available` |
| 2 | Trailer | `trailer` |
| 3 | Release date | `release_date` |
| 4 | Production status | `production_start`, `production_wrap`, `canceled` |
| 5 | Cast | `casting`, `cast_removed` |
| 6 | Crew | `crew_attached`, `crew_removed` |
| 7 | Studios | `company_attached`, `company_removed` |
| 8 | Franchise | `collection_attached`, `collection_removed` |
| 9 | Other updates | any `event_type` not listed above |

- Only catalog types ever reach Not yet reported. `announced` and `first_look` are story-only,
  so a story event is always news-backed. They therefore land in Other updates only if that
  ever changes.
- `credit_removed` lands in Other updates during the window between a frontend deploy and the
  backend migration (NR-12). That is what frees the deploy order.
- A heading with nothing under it renders nothing, the same NEU-1467 rule as the sections.
- The map and the order live in the frontend (NR-7). The order is a hand-written list, and a
  test pins that list as a literal. It cannot be derived from `_EVENT_STAGE`: Crew, Studios
  and Franchise all map to the `announced` stage and tie there, so their order is a product
  choice (people before organisations); and `canceled` is the top-ranked stage yet files under
  Production status, because a reader looks for a cancellation where a film's status lives.
  "Follows the significance ranking" means Now available through Release date and Cast come
  out as the arc ranks them, with those two exceptions.

**NR-4 — A film row per (update type, film).**
- A film with changes of two types that day appears under both headings, each time with the full
  `FeedDayCard`-style header: the linked title plus `filmParenthetical`.
- Films under a heading sort by natural title. This is the order `splitByNewsBacked` already
  produces, and the grouping keeps it stable.
- A film row's events keep the backend's order (`occurred_at, created_at, id` ascending).
- Zebra striping restarts under each heading.

**NR-5 — The event line under an update-type heading drops the beat pill.**
- The line shows the confidence pill, the summary, the edited pill and the retracted link,
  rendered exactly as an In the news line (same size, weight and colour). The heading names the
  type, so the pill would only repeat it.
- **Under Other updates the beat pill stays**, because that heading names nothing.
- This is not a third demotion part (frontend `CONTEXT.md` **Demotion**, amended). It is a
  layout difference. Size, weight, colour, the confidence badge and the link are still
  identical.
- A superseded attachment and the removal that superseded it now sit under the same heading.
  That is part of the point of NR-9.

**NR-6 — Both section headings lose their counts.**
- They read "In the news" and "Not yet reported", bare, as h3s.
- Update-type headings are bare too, as h4s, so the heading levels are h2 day → h3 section → h4
  update type.
- The rest of `SectionWrapper` stays: the rule (`SECTION_BREAK`) and the null-when-empty
  behaviour.

**NR-7 — The frontend does the grouping; no DTO change.**
- A pure function in `frontend/src/lib/feed-groups.ts` holds the type → heading map and the
  heading order beside the other grouping helpers, which keeps the order and membership
  testable without rendering. Its working name is `groupByUpdateType(items)`, returning
  `[{ key, label, rows: [{ item, events }] }]`.
- It takes the day's Not yet reported rows and returns the headings in NR-3 order. Each heading
  holds its film rows, and each row holds only that heading's events.
- The heading labels are display copy and live in `components/film/labels.ts` beside
  `NOT_YET_REPORTED_LABEL`.
- A catalog row that arrives with `events: []` (the NEU-1212 fallback, an older backend) is
  filed under the heading of each type in its `event_types`. It shows the film header with no
  event lines; its badges are dropped as redundant with the heading.
- **The grouping is section-agnostic.** The function takes any list of a day's `FeedDayItem` rows
  and never reads `news_backed`. It must not assume catalog provenance anywhere: no reliance on
  empty `sources`, and no type list limited to catalog types beyond the NR-3 map itself.
  `FeedDayGroups` is the only place that decides which section is grouped, and it passes only
  the Not yet reported rows.
- The same applies to the block that renders a grouped section (headings, film rows, event
  lines, the Now available JustWatch credit): it takes the grouped headings, not the section.
  An event line under a heading keeps its source chips when it has any.
- This is a hedge, not a plan. Grouping In the news later should be a change to the call site,
  plus Announced and First look headings, which are story-only types the NR-3 map omits
  (§2, *In the news*).

**NR-8 — JustWatch credit once, at the foot of the Now available heading.**
- `FeedDayCard`'s per-row attribution keeps applying to In the news rows.
- Under Not yet reported, the credit renders once beneath the Now available block, which is the
  only heading whose bodies name providers.

### The removal split (backend)

**NR-9 — `credit_removed` becomes `cast_removed` + `crew_removed`.**
- `cast_removed` covers a `cast` credit. `crew_removed` covers `director`, `writer` and `crew`,
  mirroring `CREDIT_ROLE_EVENT_TYPES` (`director`/`writer`/`crew` → `crew_attached`,
  `cast` → `casting`).
- Both stay `rumored`. Both are unmapped in `_EVENT_STAGE`. Both are excluded from the LLM and
  story-dedup vocabularies, exactly as `credit_removed` was.
- Distinct types were chosen over a role column (ADR-0014 amendment). `uq_event_catalog_change`
  is `(film_id, event_type, occurred_at)`, so two cards for one observation need two types.

**NR-10 — Detachments group per (film, changed_at, role class).**
- `group_detachments` (`ingest/sweep/credit_events.py`) keys on the role class as well. Each
  group cards its class's type with a body rendered from that class's credits only.
  `CreditsDetached` / `_render_detachments` in `synthesize/deterministic.py` already render per
  role, so they render the class's subset.
- Every rule from NEU-1200 / NEU-1205 applies per class, unchanged:
  - **Prior-attachment gate.** It now requires a prior visible attachment card *of the matching
    type*: `casting` for `cast_removed`, `crew_attached` for `crew_removed`.
  - **Forward-dwell hold.** It is already scoped by `role_match_key`.
  - **Supersession.** A removal marks only the attachment cards of its own class `superseded`.
  - **Removal-aware suppression.** `_latest_credit_event_types` pairs `casting` with
    `cast_removed` and `crew_attached` with `crew_removed`.

**NR-11 — Every vocabulary enumeration learns the two types.**
- `ck_event_type` (`news/models.py` and a migration).
- `news/catalog_events.py` (`CREDIT_REMOVED_EVENT_TYPE` becomes a pair, and the
  `CREDIT_EVENT_TYPES | {…}` union).
- `public/arc.py` (comment only: both unmapped).
- `app/follow_queries.py`: person-attachment pairs and first-detachment, which keep matching a
  person by name across their class's attach and removal cards.
- `app/services/digest_sender.py` `DIGEST_BEAT_LABELS` (NR-13).
- The docstrings that name `credit_removed` in `ingest/sweep/company_events.py` and
  `confirm_events.py`.

The ticket greps for `credit_removed` / `CREDIT_REMOVED` across `src/`, `tests/` and `scripts/`.
It leaves the string only in historical migrations. `scripts/` is not optional:
`scripts/backfill_credit_supersessions.py` imports `CREDIT_REMOVED_EVENT_TYPE` and would fail at
import once the constant goes, and `scripts/backfill_credit_removals.py` drives the detachment
phase and must still run.

**NR-12 — Existing `credit_removed` cards are migrated, then the type is retired.** It is one
Alembic revision, with data and constraint together:

1. Add `cast_removed` and `crew_removed` to `ck_event_type`.
2. For each `credit_removed` event, read its credits' roles from the
   `catalog.film_credit_change` rows the sweep carded it from. **Not** via
   `carded_by_event_id`: no removal card has ever set that column. Only
   `news.attachment_confirm` writes it, when it stamps a story card, and the local snapshot
   has 28 removal cards with 0 credit rows linked that way. The card's natural key is the one
   `group_detachments` grouped on: the rows at the card's `film_id` with `change = 'removed'`
   and `changed_at = occurred_at`. Restrict those rows to people the card names, by joining
   `catalog.person` and matching `normalize_name(person.name)` (`news/subject_key.py`, or its
   `sql_normalized_name`) against the card's `subject_key`: the prior-attachment gate drops
   people, so an unfiltered join over-counts (locally it reports 2 mixed cards; filtered, 1).
   Each row's class is `recorded_role(credit_type, job)` (`catalog/seed_grade.py`), `cast` or
   not.
   - **Single class** (the common case; 27 of 28 in the stale local snapshot): retype in place.
     The id, summary, timestamps and every FK reference stay as they are.
   - **Mixed**: the original event keeps its id and becomes the **cast** half. A new event is
     inserted as the **crew** half, with the same `film_id`, `provenance`, `confidence`,
     `occurred_at`, `created_at` and `status`, and with `subject_key` split by class. Both
     halves' `event_summary` rows are re-rendered from their class's credits, using the same
     deterministic renderer, `model` and `prompt_version` the sweep writes.
     - Re-point the crew-class attachment cards (`crew_attached`) whose `superseded_by` is the
       original to the new half.
     - Copy the original's `app.notification` rows (keyed
       `(user_id, event_id, kind, channel)`) to the new half with the same state, so a sent
       digest stays sent and a queued one carries both halves.
     - An edited summary (`edited_at` set) on a mixed card is re-rendered anyway, and the
       migration logs the event id. None are expected.
3. Drop `credit_removed` from `ck_event_type`.

The downgrade retypes both halves back to `credit_removed` but does not rejoin split pairs,
since `uq_event_catalog_change` would reject two `credit_removed` cards at one `occurred_at`.
It therefore deletes the crew half of each split pair after re-pointing its references back.
Document it as lossy for the crew half's summary.

If a `credit_removed` card has no matching `film_credit_change` row for one of the names on its
`subject_key` (none locally), the migration **fails** naming the card rather than guessing: the
history table is the only record of who was which class, and a summary parse would break on a
name with a period in it ("D.C. Shen" is on the one mixed card locally). Fix the data and re-run.

**The renderer in a migration.** "The same deterministic renderer, `model` and `prompt_version`
the sweep writes" means importing `render_summary` / `CreditsDetached` / `CreditDetached`,
`DETERMINISTIC_MODEL` and `TEMPLATE_VERSION` from `synthesize.deterministic` inside the
revision. Nine existing revisions already import from `upmovies`, so that is allowed, and it is
accepted here on purpose: a later wording change to the renderer changes what a *re-run* of this
migration writes, but the migration runs once per database, and the alternative, copying four
phrasings into the revision, drifts in the other direction. The migration writes the
`news.event_summary` rows itself (insert for the crew half, update for the cast half, same
`source_updated_at` as the original); the sweep's `write_deterministic_summary` is async and
refuses an edited row, so it is not called.

### Labels

**NR-13 — The new beat labels are "Cast departure" and "Crew departure".**
- They are the same strings in the frontend `EVENT_TYPE_LABELS` (`components/film/labels.ts`)
  and the backend `DIGEST_BEAT_LABELS`.
- They show wherever a removal renders with a beat pill: In the news, once a story attaches;
  the film page; the digest; and the Other updates fallback before the migration.

**NR-14 — The frontend label map adopts the digest's wording.**
- The map's comment claims it matches `DIGEST_BEAT_LABELS` "exactly". It does not today:

  | Type | Frontend today | Digest |
  |---|---|---|
  | `trailer` | Trailer | New trailer |
  | `production_start` | Production start | Production started |
  | `production_wrap` | Production wrap | Production wrapped |
  | `credit_removed` | (missing) | (entry exists) |

- The frontend takes the digest's strings. After this project the two maps are identical: both
  carry `cast_removed` / `crew_removed`, and neither carries `credit_removed`, which no longer
  exists. During the deploy window, a stray `credit_removed` reads through `eventTypeLabel`'s
  title-case fallback as "Credit Removed".
- A test pins the frontend map to a copied list of the digest's keys and strings, so the drift
  is caught where the frontend can see it.
- **`other` leaves the frontend map.** Today `EVENT_TYPE_LABELS` carries `other: "Update"` and
  the digest map does not (its `digest_beat_label` *falls back* to "Update" for any unknown
  type). Key-for-key equality means the frontend drops the entry: `other` is in
  `HIDDEN_EVENT_TYPES` and never reaches a surface, nothing in the frontend reads the key, and
  if one ever did arrive the title-case fallback would read "Other", which is no worse. The two
  maps are then literally equal.
- **Update-type headings are not beat labels.** "Trailer" the heading and "New trailer" the
  pill are different strings on purpose: a heading names a kind, a pill names a beat.

## 5. Deploy order

There is none. The Other updates heading (NR-3) files any type the frontend does not map, with
its beat pill, so the two repos can deploy in either order:
- **Frontend first.** `credit_removed` sits under Other updates until the migration runs.
- **Backend first.** `cast_removed` / `crew_removed` sit under Other updates with title-case
  pills until the frontend ships.

## 6. Milestones and tickets

- **M1 — Removals split by role** (backend). One ticket, **NEU-1518** (story NEU-1515), covers NR-9 to NR-12 plus the backend
  half of NR-13. Splitting it would leave a revision where `credit_removed` cards can no longer
  be written or can no longer be read.
- **M2 — Not yet reported by update type** (frontend). Two tickets:
  - **NEU-1519** (story NEU-1516) — the layout: NR-1 to NR-8.
  - **NEU-1520** (story NEU-1517) — the label map: the frontend half of NR-13, and NR-14.
    It is blocked by NEU-1519 only because both edit `components/film/labels.ts`.

  Neither needs anything from M1 (§5).

Linear project: https://linear.app/neuroticsasquatch/project/bl-not-yet-reported-by-type-c8cdaf65f491
(`P-NEU-94`). The milestone descriptions carry the shared contracts.

## 7. Owed around merge

- **Before NEU-1518 deploys**, not after: a real-data check of the migration against a prod
  snapshot. Count the mixed cards with the NR-12 query (the local snapshot is stale at
  2026-09-01 and had one), confirm every name on every card matches a credit row, and confirm
  none has `edited_at` set. It is a dry run of the migration's own reads, so it belongs before
  the revision runs for real.
- An eyeball of a backfill-tall day, ADR-0016's 70+ row case, on a phone. Update-type headings
  should make it shorter to scan, not longer.
- Optional, not ticketed: frontend docblocks that misdescribe backend order. `groupByDay` says
  `last_created_at DESC`, and `FeedDayPosters` / `dayPosterLeads` say "by popularity". The
  backend actually orders day → significance → natural title. Fix them if the layout ticket
  touches those files.

## 8. Revisions

- **2026-10-01, readiness review.** NR-12 had the migration reading roles through
  `film_credit_change.carded_by_event_id`, a column no removal card sets; it now reads the
  `(film_id, 'removed', changed_at = occurred_at)` rows filtered to the card's `subject_key`,
  and fails rather than parsing summaries. NR-11's grep gained `scripts/`. NR-3 records that
  the heading order is a pinned literal, not a derivation from `_EVENT_STAGE`. NR-14 decides
  that `other` leaves the frontend map. §7's prod-snapshot check moved to before deploy.
