# bl: Feed by Followed Entity — project spec

**Status:** approved design, scaffolded in Linear by `/personal:projectit` (2026-10-02)
**Builds on:** ADR-0019 (a follow is an attachment stream; EF-3's where-clause is unchanged),
ADR-0016 (publication-day grouping), ADR-0021 (the digest is the only delivery), NEU-1199 (one
row per film, day and section), the Not Yet Reported by Type project (NR-1 to NR-8, the
update-type layout this reuses), the Digest Content project (DC-1 to DC-17, which §4 amends).
**Repos:** `upcoming-movies-backend` (the row contract, the mail), `upcoming-movies-frontend`
(the timeline layout).
**Glossary:** backend `CONTEXT.md` — **Reach**, **Follow block**, **Entity row / entity entry**
added; **Timeline**, **Digest**, **Slate**, **Lead film**, **Film entry** rewritten. Frontend
`CONTEXT.md` — **Follow block**, **Entity row** added; **Update type**, **Not yet reported**
amended. Use those terms.
**ADR:** ADR-0022 (a timeline row is keyed by its reach; the digest reproduces the timeline).

This document is the project-wide spec that `/personal:implementit` falls back to for any
ticket in the project (ADR-0004 of the personal plugin). It records the outcome of the
project-shaping interview held with Tom on 2026-10-02 (a `/grilling` pass inside
`/projectit`): every decision, what it changes about what ships today, and the milestone
contracts.

---

## 1. Purpose

The timeline ("My feed", `/`) is the grouped feed filtered to the reader's follows, and it is
laid out exactly as the feed is: a day, two provenance sections, one row per film. That layout
is right for a followed film — the film is the subject. It is wrong for a followed person,
studio or franchise. Their follow delivers attachment cards (ADR-0019), and the timeline shows
each as a row headlined by a film the reader may never have heard of, with the entity they
actually follow named only inside the summary line. The digest has the same shape and papers
over it with a "Following: …" line.

This project lays each timeline day out by the kind of follow that delivered its rows —
**Films, People, Studios, Franchises** — and, under the three entity blocks, headlines each row
with the followed entity. Each line under an entity names the film first (title and
parenthetical, linked), then the summary. Each block keeps the feed's In the news / Not yet
reported split; under an entity block, Not yet reported is laid out as Attached, Detached,
Canceled. A card that reached the reader two ways appears in each block that reached it.

The digest follows the timeline. The daily becomes the timeline day reproduced in mail; the
weekly keeps one entry per film or entity across the week under the same blocks and sections;
the slate becomes the my-films calendar reproduced for its dates. The lead card, the entry cap
and the "Following:" line go.

## 2. Scope

**In**

- `GET /me/timeline`: rows keyed by reach, with a nullable `via` (FB-12 to FB-17).
- The timeline page's day layout: follow blocks, entity rows, the entity update types (FB-1
  to FB-11). New grouping helpers beside the existing ones in `lib/feed-groups.ts`.
- The digest sender and both templates, both cadences, both parts (FB-18 to FB-27).
- The slate's rendering (FB-26).
- Glossary and ADR (done in the planning session: docs-only PRs, no ticket id).

**Out**

- **The global feed** (`/feed`, `GET /feed/grouped`). Unchanged. Its rows carry `via: null`
  because the DTO is one type, and nothing else about it moves. `FeedDayGroups` stays its
  renderer.
- **The film page**, the entity pages and their Recent activity cards, `/me/follows`,
  `last_activity_at`, the notify pass, `app.notification` rows. The attribution builder they
  share (`follow_attribution_pairs`) is read, not changed.
- **What a follow delivers.** EF-3 and EF-13 stand. Nothing here adds or removes a card from
  anyone's timeline; it changes where a card sits and what headlines it.
- **The subject rule** (DC-7), the preheader (DC-10), the unsubscribe mechanics (DC-10
  headers), the admin preview and test-send routes (DC-11), the slate window and markers
  (DC-9), `SLATE_WEEKDAY`.
- The follow blocks' *order* is fixed and not a setting. No per-block toggles, no filters.
- Entity rows carry no image. The follows page has `image_path`; the feed row has no picture
  and the entity row matches it. Open item, §8.
- In the news stays laid out by film (or entity) row, not by update type. The NR deferral
  stands.

## 3. Today, in one paragraph

`service.get_timeline` calls `get_feed_grouped` with `film_filter=title_follow_film_ids` and
`event_filter=entity_attachment_event_ids`, OR-ed by `_feed_scope`, and answers with
`FeedDayResponse`: one `FeedDayItem` per (film, day, section), each carrying only the events the
scope let through. A film-day reached by both filters is one row holding both kinds of events;
a film-day reached by an attachment alone is one row holding that attachment. `TimelinePage`
hands the items to `FeedDayGroups`, the same component the global feed uses, which buckets by
day, splits on `news_backed`, and renders In the news as `FeedDayCard` rows and Not yet reported
through `groupByUpdateType` / `UpdateTypeGroups`. Nothing on the row or the page knows which
follow produced it. The digest (`digest_sender`) groups the user's queued rows by film into
`DigestEntry` values, ranks them, renders the first as a lead card and the rest compact, cuts at
20, and names the entity follows that reached an entry on a "Following:" line built from
`follow_attribution_pairs`. The slate is a flat dated list of title + release label.

## 4. Decisions

Decision ids are **FB-n**. Where one amends an earlier decision it says which.

### The timeline layout (frontend)

**FB-1 — A day is laid out in follow blocks.** Under each day heading, in this fixed order:
**Films**, **People**, **Studios**, **Franchises**. Each block holds the feed's two sections,
In the news then Not yet reported (unconfirmed). The heading ladder is h2 day → h3 block → h4
section → h5 update type. A block with no rows that day renders nothing (the NEU-1467 silence
rule, one level up). The Films heading renders whenever the Films block has rows, including on
a day it is the only block: every day reads the same.

```
Thursday, October 2, 2026
  [poster strip — the whole day's films]
  Films
    In the news
      Dune: Part Three (US, Dir: Villeneuve, 2026)
        [Casting] [confirmed] Zendaya returns as Chani.  ·  Variety
    Not yet reported (unconfirmed)
      Release date
        Heat 2 (US, Dir: Mann, 2027)
          US Wide release date slipped from Aug 6 to Nov 12.
  People
    In the news
      Denis Villeneuve
        [confirmed] Rendezvous with Rama (US, 2028) — Villeneuve will direct.  ·  Deadline
    Not yet reported (unconfirmed)
      Attached
        Florence Pugh
          The Thomas Crown Affair (US, Dir: Jordan, 2027) — Florence Pugh joins the cast.
      Canceled
        Denis Villeneuve
          Cleopatra (US, Dir: Villeneuve) — The film has been canceled.
  Studios
    Not yet reported (unconfirmed)
      Attached
        Legendary Pictures
          Dune: Part Three (US, Dir: Villeneuve, 2026) — Legendary Pictures joins as a production company.
```

**FB-2 — The Films block is today's layout over title follows only.** It renders exactly what
`FeedDayGroups` renders today for a day — `FeedDayCard` rows under In the news,
`groupByUpdateType` / `UpdateTypeGroups` under Not yet reported — over the rows whose `via` is
null. A film-day reached only by an entity follow is **not** in the Films block any more; it is
in the entity's row. A film the reader follows by title shows its whole day here as the feed
would, including the attachment card that also appears under an entity row.

**FB-3 — An entity row is one (entity, day, section).** Under People, Studios and Franchises:
- The headline is the entity's name, linked to `/person/:ref`, `/studio/:ref` or
  `/franchise/:ref`, in the row-title weight `FeedRowTitle` uses. No parenthetical, no image.
- Under it, one line per card, in the event-line style (`FeedEvent`): the film's title and
  parenthetical as **one link** to `/film/:ref`, in the row-title weight, then the summary in
  the normal event text, then the edited pill and the retracted link as today. Source chips
  beneath the line when it has any.
- The confidence pill shows under In the news and drops under Not yet reported, as NR-5.
- Zebra striping restarts under each heading, as in the Films block.
- A row never carries the JustWatch credit: `now_available` cannot reach an entity row (EF-3).
  If an unmapped type ever does, it files under Other updates with its beat pill (FB-4) and
  nothing else special.

**FB-4 — Entity update types.** Under an entity block's Not yet reported, the headings are:

| # | Heading | `event_type`s |
|---|---|---|
| 1 | Attached | `casting`, `crew_attached`, `company_attached`, `collection_attached` |
| 2 | Detached | `cast_removed`, `crew_removed`, `company_removed`, `collection_removed` |
| 3 | Canceled | `canceled` |
| 4 | Other updates | any `event_type` not listed above |

- "Attached" / "Detached" are the words `labels.ts` already uses for the studio and franchise
  beats ("Studio attached"), so they are on-screen vocabulary already. `canceled` is neither a
  join nor a leave, and ADR-0019 rejected modelling a cancellation as a detachment: its own
  heading, last.
- Beat pills drop under Attached, Detached and Canceled, as NR-5 drops them under a heading
  that names the type. "Attached" under People does not say cast vs crew; the summary does
  ("joins the cast as X", "attached as director"). Under Other updates the pill stays.
- The map and the order are a second literal beside `UPDATE_TYPES` in `lib/feed-groups.ts`,
  pinned by a test as NR-3's is; the labels live in `components/film/labels.ts` beside
  `UPDATE_TYPE_LABELS`. `groupByUpdateType` takes the map as a parameter rather than growing a
  second function: it stays section-agnostic (NR-7) and becomes block-agnostic too.
- A film-reach row (`via: null`) never sees this map; it uses NR-3's. The two maps never merge:
  `casting` is Cast under Films and Attached under People.

**FB-5 — A card shows once per follow that reached it.** A cancellation of a film whose
director and studio the reader follows is a line under the director's row and a line under
the studio's row. If the reader also follows the film by title it is a line under the film's
row in the Films block as well. This is what "one row per reach" means (ADR-0022) and it is
deliberate: each block answers "what happened to the things I follow of this kind", and a
block that silently lost a card because another block showed it would answer wrongly.

**FB-6 — Ordering.**
- Blocks: fixed (FB-1).
- Rows within a Films section: as today (natural title under In the news; NR-4 under Not yet
  reported).
- Entity rows within a section or an update type: by name, A–Z. People by name as written
  (casefolded, no article stripping — "The" is not how a person's name starts); studios and
  franchises by the natural sort `splitByNewsBacked` applies to titles (leading "A", "An",
  "The" ignored). Ties on name break on `entity_id`.
- Lines within an entity row: by film natural title, then the backend's event order
  (`occurred_at, created_at, id` ascending). Under an update type, a row holds only that
  type's lines (NR-4's rule, per entity).
- The sort is the frontend's (a pure helper), as the title sort is today. The backend's row
  order is only required to keep a day's rows contiguous (FB-13).

**FB-7 — One poster strip per day, over every block.** `FeedDayPosters` takes the day's full
item list, title and entity rows alike; `dayPosterLeads` already de-duplicates by `film_ref`
and orders news-backed first, and nothing about that changes. A day with only entity rows
still has a strip. Posters are the film's; an entity row contributes its film.

**FB-8 — The timeline gets its own day renderer; the feed keeps `FeedDayGroups`.**
- `TimelinePage` renders a new `TimelineDayGroups` (name indicative) instead of `FeedDayGroups`.
  It buckets by day (`groupByDay`, unchanged), renders `FeedDayPosters`, then for each follow
  block that has rows: the block heading, then `SectionWrapper` for each section (exported from
  `FeedDayGroups.tsx` or moved to a shared module), with the Films block delegating to exactly
  the row components the feed uses and the entity blocks to an `EntityRow` component.
- New pure helpers in `lib/feed-groups.ts`, each with its own test: `groupByFollowBlock(items)`
  → `[{ key: "films" | "people" | "studios" | "franchises", label, items }]` in FB-1 order,
  empty blocks omitted, keyed on `via?.entity_type` (`null` → films, `person` → people,
  `company` → studios, `franchise` → franchises); `groupByEntity(items)` → one entity row per
  `(entity_type, entity_id)` holding its rows' events as lines tagged with their film, sorted
  per FB-6; `groupByUpdateType(items, map)` as FB-4.
- `UpdateTypeGroups` takes a row renderer (or a block kind) so one component lays out film rows
  under the Films block and entity rows under the others. The JustWatch-under-Now-available
  rule (NR-8) only ever fires for the Films block.
- `FeedDayGroups`, `GlobalFeed` and the global feed's tests are untouched.

**FB-9 — Row identity and tolerance.** The React key of a row is `${reach}:${film_ref}`
where `reach` is `title` for `via: null` and `${entity_type}:${entity_id}` otherwise. A row
with **no `via` field at all** (an older backend) is a title row: the page renders every row
in the Films block, which is today's layout. This is what lets the frontend deploy first (§5).
`via` is typed optional-and-nullable in `api/types.ts` for exactly that reason, with a comment
saying so.

**FB-10 — An entity the catalog cannot name.** `via.name` null (a person purged from TMDB, a
follow row older than a backfill — the `FollowOut` cases) renders the headline as a type
fallback — "A person you follow", "A studio you follow", "A franchise you follow" — unlinked
(`via.ref` is null too). The row still renders; the follows page lists such a follow with
nulls rather than dropping it, and the timeline does the same.

**FB-11 — Unchanged copy and chrome.** `SECTION_SPLIT_EXPLAINER` stays as it is and where it
is: "Each day leads with what the trades have covered" is still true of every block. The empty
timeline, the lapsed-grant panel, the "All updates →" link, the "View more" paging and the
timeline hint are untouched.

### The contract (backend)

**FB-12 — `FeedDayItem.via`.** One new field on the existing DTO:

```python
class FeedVia(BaseModel):
    entity_type: Literal["person", "company", "franchise"]
    entity_id: str            # the follow graph's id text (TMDB id as text)
    name: str | None          # the entity's current name; None when unresolvable (FB-10)
    ref: str | None           # `<id>-<slug>`, as the entity pages' routes take it; None with name

class FeedDayItem(BaseModel):
    ...
    via: FeedVia | None = None   # None on the global feed and on title-reach rows
```

- `entity_type` uses the follow graph's words (`company`, `franchise`), as `FollowOut` does;
  the frontend maps them to `/studio` and `/franchise` as the digest's `_FOLLOWING_ROUTES`
  does. "Studio" is screen vocabulary only (EF-19).
- `ref` is built by the same helpers the entity pages' canonical redirects use (`person_ref`,
  `company_ref`, `collection_ref`), so a timeline link never 301s.
- `FeedDayResponse` is unchanged. `/feed/grouped` ships `via: null` on every row.

**FB-13 — Row grain and composition.** A timeline row is (reach, film, day, section).
`get_timeline` composes two row sets over the same day window:

- **Title rows:** `get_feed_grouped(film_filter=title_follow_film_ids(user_id))` exactly as
  today — the film's whole visible day — with `via=None`.
- **Entity rows:** from `_entity_event_pairs` (through a public builder; `follow_attribution_pairs`
  already projects `(entity_type, entity_id, event_id)` and is the one the digest reads —
  the timeline reads the same pairs, so the page and the mail cannot disagree), joined to
  `news.event` under the feed's visibility terms, grouped by
  `(entity_type, entity_id, film, day, has_story)`. Each row's `events`, `event_count`,
  `event_types` and `top_event_type` are computed over **its own** events (NEU-1199's
  contract: `event_types` always matches the row's events), so an entity row holds only the
  cards that reached the reader through that entity.
- **Day pagination** stays by distinct day over the union of both reaches — the existing
  `_feed_scope` OR is still the right day-window clause — so `total`, `limit` and `offset`
  mean what they mean today and a page is still "N days".
- **Order:** `day DESC` first, so `groupByDay`'s adjacency bucketing holds; within a day, title
  rows (today's significance-then-title order) then entity rows (`entity_type` in
  person/company/franchise order, then name, then film title). The frontend re-sorts entity
  rows anyway (FB-6); the backend order is for stable output and tests.
- Title rows are **not** de-duplicated against entity rows (FB-5), and the two sets are
  disjoint by construction: a title row has `via=None`.
- `_feed_scope`'s OR survives only for the day window and the day count. The old single OR-ed
  row query is what this replaces.

**FB-14 — Visibility is the feed's.** Entity rows take `visible_events()` / `feed_visible()`,
the `EventSummary` join and the slug term exactly as the OR-ed query gave them; the pair
builders carry no visibility of their own and must not grow any.

**FB-15 — Name and ref resolution is batched per page.** One lookup per entity type over the
page's distinct `(entity_type, entity_id)` pairs, through the catalog tables
(`catalog.person`, `catalog.production_company`, `catalog.collection`). The digest's
`_load_following` does this already; hoist that lookup into a shared helper (for example
`app/entity_names.py`) that the timeline, the digest and — optionally, not in scope —
`/me/follows` can call. Never per row.

**FB-16 — Nothing else in the backend moves.** `entity_attachment_event_ids`,
`follow_last_activity`, the notify pass, `GET /me/follows`, the entity pages' `/events` routes
and the global feed are unchanged. The `canceled`, first-association and organisation branches
are read as they are.

**FB-17 — The router's docstring and the service's `get_timeline` docstring** say what a row is
now: the "one way a timeline row is not its feed row" paragraph becomes "a timeline row is a
feed row with a reach", and the NEU-1365 explanation of the OR becomes the day-window note.

### The digest (backend mail)

Amends the Digest Content project: DC-3, DC-4, DC-6 and DC-8 are superseded as noted; DC-1,
DC-2, DC-5 (the line form), DC-7, DC-9 to DC-17 stand unless a decision below says otherwise.

**FB-18 — The daily is the timeline day, reproduced.** The batch's queued rows are grouped by
**publication day** (UTC `created_at`, ADR-0016 — the feed's axis), newest day first; one feed
day per day the batch spans (a batch after a missed send spans several and renders several).
Each day renders, in order: the day heading (`day_heading`), the poster strip, then the follow
blocks, sections, update types and rows of FB-1 to FB-6, with the row forms of FB-20.
*Supersedes DC-3's "the mail does not repeat a film per day" for the daily.*

**FB-19 — The weekly reads by entry, not by day.** The same blocks, sections and update types
as the daily, but one **film entry** per film (Films block) and one **entity entry** per entity
(the other three) across the week, under each section and update type they touched; an entry's
lines run in publication order and carry their date. Entries order as FB-6 orders rows, with
one exception kept from DC-3: film entries under a Films section rank by their most significant
beat, then title. One poster strip at the top of "New on your timeline" (the week's films,
news-backed first, de-duplicated, same cap as a day's). A film with cards in both sections
during the week appears in both, as it would on two feed days.

**FB-20 — Line and row forms in mail.** The feed's, with dates where the feed's day heading is
missing:
- A **film row** (Films block): title + parenthetical, both linking to the film page, as DC-4
  — without the poster (the strip has it) and **without the status line** (DC-4's headline
  release). The feed row carries no status line and the mail reproduces the feed. *Supersedes
  DC-4 in those two parts.* Flagged as reversible: if the status line is missed, it is one
  template line to restore.
- A **beat line** under a film row: under In the news, DC-5 as built —
  `{date} · {Beat label} · {summary}`, Unconfirmed marker, source line beneath — with the date
  omitted in the daily (the day heading says it) and kept in the weekly. Under Not yet reported,
  the beat label drops under a named update type and stays under Other updates (NR-5), the
  Unconfirmed marker drops (the section heading carries "(unconfirmed)"), and the "via TMDB"
  source line drops (the heading says it); a story-backed line keeps its outlet link.
- An **entity row / entry**: the entity's name linking to its page, then one line per card:
  `[{date} · ]{Film} ({parenthetical}) · {summary}`, the film linked, source line beneath under
  In the news. Same date rule.
- Under Not yet reported's Now available, the JustWatch line once at the foot of the heading
  (NR-8); under In the news, per film row that has a `now_available` line (DC-17).
- A "See the film page" link per film row is dropped: the title is the link, as on the feed.

**FB-21 — Retired from the mail.** The lead card (the top-ranked entry rendered large), the
20-entry cap and its overflow line (DC-8: nothing is cut; a feed day is never truncated and
the mail is not either), the "Following:" line (DC-6: a film entry is a title follow by
construction, and an entity's cards are the entity entry's), the per-entry poster, the status
line (FB-20). `DIGEST_MAX_ENTRIES` goes. `DigestFollowing` and `_load_following` go or are
folded into FB-15's helper. The `lead` / `entries` / `overflow_line` template context is
replaced by the day and block model (FB-25).

**FB-22 — The subject and preheader rules stand (DC-7, DC-10).** *(Amended 2026-10-08 by
NEU-1543: the slate leaves the subject and the preheader; the slate-only forms change — see
DC-7, DC-10.)* The lead film is still the
film carrying the most significant beat in the mail (`rank_entries`' key, over every row of
every reach — a film reached only through a studio can lead), `N` counts the other distinct
**films** in the mail (not rows: a film under two entity rows is one film), and the preheader
names the next two films by the same ranking. The slate-only forms are unchanged. The lead
film is **not** rendered differently anywhere in the body.

**FB-23 — Markers and pills in mail** copy the feed's: the Unconfirmed marker as the amber
pill (DC-5), beat labels as plain bold text (DC-5) where they show at all; headings as plain
text at descending sizes. No new imagery beyond the strip.

**FB-24 — The text part mirrors the structure.** Day → block → section → update type → row →
line, with the same omissions. Posters are omitted; each film row and each entity line carries
its link on the line.

**FB-25 — The sender's model.** `DigestEntry`'s role is taken by a day/block tree built in the
sender from the same queued rows plus `follow_attribution_pairs`: per row-reach, as FB-13 —
title reach from the title arm, entity reaches from the entity arms — then grouped (daily) by
day → block → section → update type → row, or (weekly) by block → section → update type →
entry. The grouping and ordering are pure functions in the sender, unit-tested on their own
as `rank_entries` is today. The admin preview and test-send (`render_digest`,
`send_test_digest`) are unchanged in signature and route; the `/admin/digest` page renders
whatever they return. The backend's `film_parenthetical`, `day_heading`, `short_date` and
`digest_beat_label` helpers are reused as they are.

**FB-26 — The slate is the my-films calendar, reproduced.** *(Amended 2026-10-08 by NEU-1543:
7 days; split by calendar kind — In theaters, then At home, an empty kind omitted, the home
kind with no bucket sub-heading; no month heading; the slate follows the timeline.)* For the
slate window (DC-9, 30
days): date heading (long date, as today) → release-type bucket sub-heading (the calendar's
labels and order, `_calendar_type_rank`) → the calendar's film row: poster (`w92`), title
(year), `Dir. …`, top-three stars, up-to-three genres — the `CalendarItem` fields, built by the
same `_calendar_page` builder with `title_follow_user_id`, so the slate cannot describe a film
differently from the calendar page. The **New / Moved marker stays**, as a pill after the
title: the calendar page has no equivalent, and the mail is where a moved date is news. A month
heading only where the window crosses a month boundary; no year heading. The "Your slate" h2
and its "The next N days" line stay. The text part lists date → bucket → one line per film.
`load_slate_markers` is unchanged. *Supersedes DC-9's rendering, not its scope.*

**FB-27 — DC-1, DC-2, DC-13 stand.** The daily is still "yesterday on your timeline", nothing
to say is still no mail, and the slate day still puts the slate in front. *(Amended
2026-10-08 by NEU-1543: the slate follows the timeline.)*

## 5. Deploy order

- **Frontend (M2) before, or together with, backend (M1).** The new backend emits two rows for
  a film-day reached both ways and entity-only rows the old `FeedDayGroups` would render as film
  rows; the old frontend keys rows on `film_ref` and would both duplicate films and collide on
  keys. The new frontend treats a row without `via` as a title row (FB-9), so it renders today's
  layout against the old backend.
- **Mail (M3) after M1 merges.** Same repo; M3 reads the shared helper and the row composition
  M1 introduces. No deploy coupling with the frontend.
- No migration, no Coolify variable, no data backfill.

## 6. Milestones and tickets

Proposed at Phase 2 and confirmed at Phase 3 of the scaffolding session; the milestone
descriptions in Linear carry the shared contracts.

- **M1 — The row knows its reach** (backend): FB-12 to FB-17. One ticket; splitting the
  contract from the composition would leave a revision that ships `via` always null.
- **M2 — The timeline by follow block** (frontend): FB-1 to FB-11. Two tickets: the grouping
  helpers and entity update-type map (pure, tested), then the page and components.
- **M3 — The digest reproduces the timeline** (backend mail): FB-18 to FB-27. Three tickets:
  the sender's row/day/block model with the daily templates; the weekly on that model; the
  slate as the calendar. The first blocks the second; the slate is independent.

### Ticket map (Linear, created 2026-10-02)

Linear project: https://linear.app/neuroticsasquatch/project/bl-feed-by-followed-entity-61bfd25b952c
(`P-NEU-95`). The milestone descriptions carry the shared contracts.

| Milestone | Story | Ticket | Repo | Blocked by |
|---|---|---|---|---|
| M1 | NEU-1521 | **NEU-1525** `feat(timeline): key rows by reach, add via` — FB-12 to FB-17 | backend | — |
| M2 | NEU-1522 | **NEU-1526** `feat(feed): follow-block and entity grouping helpers, entity update types` — FB-4, FB-6, FB-8 (helpers), FB-9, FB-12 (type) | frontend | — |
| M2 | NEU-1522 | **NEU-1527** `feat(feed): timeline day laid out by follow block` — FB-1 to FB-3, FB-5, FB-7, FB-8 (components), FB-10, FB-11 | frontend | NEU-1526 |
| M3 | NEU-1523 | **NEU-1528** `feat(mail): digest rows by reach, daily as the timeline day` — FB-18, FB-20 to FB-25, FB-27 | backend | NEU-1525 |
| M3 | NEU-1523 | **NEU-1529** `feat(mail): weekly digest by entry under follow blocks` — FB-19 | backend | NEU-1528 |
| M3 | NEU-1524 | **NEU-1530** `feat(mail): slate as the my-films calendar` — FB-26 | backend | NEU-1528 (template conflicts only) |

All six are `loop-ready`.

## 7. Owed around merge

- A real digest, both cadences, opened in Gmail and in one other client (Outlook or Apple
  Mail), with screenshots on the PR (gh cannot upload; attach by hand). The heading ladder is
  five deep and nested tables are where mail clients disagree.
- A phone eyeball of a timeline day with all four blocks populated, and of a backfill-tall day
  (ADR-0016's 70+ row case) under the Films block.
- The admin preview (`/admin/digest`) for one user with entity follows, before and after M3.
- Confirm on prod data that `follow_attribution_pairs` for a heavy follower (a few hundred
  follows) answers in the same time it does for the digest; the timeline reads it per page now.

## 8. Open items (not blocking)

- **Entity row images.** `FollowOut` has `image_path`; the feed row has none and the entity
  row matches it. Revisit if the People block reads as a wall of names.
- **In the news by significance.** The NR deferral stands; nothing here changes
  `splitByNewsBacked`'s title sort.
- **`/me/follows` film rows cannot link** (pre-existing, unticketed). FB-15's helper could
  serve that page a `ref`; not in scope.
- **The weekly's film entries keep DC-3's significance rank** within a section while entity
  entries sort by name. If that reads as inconsistent, name-sort both.

## 9. Revisions

- **2026-10-05, NEU-1533.** FB-7's "orders news-backed first, and nothing about that changes",
  and FB-19's "news-backed first" for the weekly strip, are superseded: the strip's order is
  the day's (or the week's) **reading order**, each film at its first appearance, block by
  block, In the news before Not yet reported, update type by update type, row by row and line
  by line. `FeedDayPosters` takes the day's layout (`layoutTimelineDay`), not its flat items,
  and the digest's `day_posters` walks the blocks. On the timeline a Films-block film's poster
  now leads a People-block news film's. See `frontend/docs/specs/NEU-1533-poster-strip-reading-order.md`.
