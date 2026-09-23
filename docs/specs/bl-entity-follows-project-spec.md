# bl: Entity Follows — project spec

**Status:** approved design, scaffolded in Linear by `/personal:projectit` (2026-09-20)
**Supersedes:** `docs/specs/bl-consumer-pivot-project-spec.md` D-42 to D-50 in part, and D-11,
D-13, D-27, D-31, D-32 where noted; `docs/adr/0018-follow-subsumes-the-watchlist.md` in part
(see `docs/adr/0019-a-follow-is-an-attachment-stream.md`)
**Repos:** `upcoming-movies-backend` (primary), `upcoming-movies-frontend`
**Glossary:** `CONTEXT.md` — section *Follows, timeline and delivery* rewritten for this
project; **Coverage**, **Watchlist**, **Mute** retired; **Attachment**, **Studio**,
**Last activity** added. Use those terms.
**ADR:** `docs/adr/0019-a-follow-is-an-attachment-stream.md`

This document is the project-wide spec that `/personal:implementit` falls back to for any
ticket in the project (ADR-0004 of the personal plugin). It records the outcome of the
project-shaping interview held with Tom on 2026-09-20 (a `/grilling` pass, then `/projectit`):
every decision, the contradictions with accepted behaviour each one resolves, and the milestone
contracts. Where it is silent, the Consumer Pivot project spec governs, and where *that* is
silent, `docs/specs/backlotter-consumer-pivot-spec.md`.

---

## 1. Purpose

A follow on a person, studio or franchise stops meaning "alert me about every film they touch"
and starts meaning "tell me when they join or leave a film". Following a film is the only way
to hear everything about it.

The Consumer Pivot's M8 and M9 (ADR-0018, D-42 to D-50) made every follow *cover* films: a
person follow put that person's films on a computed watchlist at a chosen coverage tier, and
every beat on those films alerted. Tom reopened that on 2026-09-20 and chose the alternative
ADR-0018 had rejected, sharpened: an entity follow is a stream of the entity's own attachment
and detachment events, nothing more. The consequences run through the whole subscriber
surface — tiers go, the watchlist stops being a computed set, mutes lose their reason, the
film page keeps one button, entity pages become the only place a follow starts — and through
ingest, which has to observe studio and franchise membership it never tracked, treat film
admission as an attachment, and resolve studios and franchises in the news.

## 2. Scope

**In**

- The follow model: binary follows for every entity type; migration off coverage tiers.
- Observation: company and collection change tracking and event types; a `canceled` event;
  admission-as-attachment for followed entities; the recorded grade widened to every followed
  person; the followed sweep tranche switched on.
- Delivery: what each follow type puts on the timeline, in the digest and on push and email.
- News: extraction and resolution of studios and franchises; the first-association rule for
  story mentions of any entity.
- Surfaces: company and collection pages and the header entity search (NEU-1420, moved into
  this project); the follows page rework; the film page reduced to one button; the watchlist
  page, the Muted section and the mute endpoints removed; entity pages showing the entity's
  own cards.
- Imports: the ratings and favorites path removed; the alert-window filter; the two-phase
  review step.

**Out**

- Anything about entitlement, signup or billing (D-37 to D-41 stand; *bl: Subscription &
  Billing* owns pricing).
- New beat types beyond `canceled` and the four company and collection attach and detach
  types.
- Story-driven film admission (the *bl: Undated Film Discovery* line).
- Widening the recorded grade to every credit on every *followed film* (title-follow pushes
  stay at seed grade, EF-9; noted as a contained follow-up).
- Any retroactive attachment history: existing credits, companies and collections are the
  baseline; only changes observed after each ticket deploys card.

## 3. Goals and acceptance (project level)

1. A subscriber following a director, a studio or a franchise sees exactly one kind of thing
   from that follow: cards saying the entity joined, left, or was announced on a film, plus
   the film's cancellation. Nothing else about those films reaches them through that follow.
2. A subscriber following a film sees every beat on the timeline and in the digest, and is
   pushed on D-32's beats, on `canceled`, and on seed-grade cast and crew joining or leaving.
3. Every followable entity has a page that holds its follow button and shows what a follow
   would deliver; the film page has one follow button and links every name to its page.
4. The follows page is one flat, filterable, sortable list with real names on every row.
5. Nothing in the codebase, the schema or the glossary still says coverage, watchlist item or
   mute.
6. A Letterboxd or TMDB import creates title follows only, only for films in the alert
   window, and only after the user has reviewed the list.

## 4. What already exists (do not rebuild)

- **Credit history and quarantine** (`catalog.film_credit_change`, D-3, D-7, D-8, NEU-1418's
  recorded grade): the attach and detach machinery for people is complete, including burst
  grouping, sanity holds, supersession (D-2) and the `crew` role. This project widens who is
  recorded (EF-2) and adds a baseline exception (EF-4); it does not touch the pipeline.
- **Status field events** (`ingest/sweep/field_events.py`): `film_field_change` already records
  every non-denylisted column, including `status` and `collection_id`;
  `STATUS_EVENT_TYPES` maps two statuses to events and deliberately drops `Canceled`. EF-6 adds
  a mapping; EF-5's collection events are one more `TRACKED_FIELDS` entry.
- **Join rebuild** (`ingest/tmdb/upsert.py::_rebuild_joins`): production companies are deleted
  and re-inserted on every upsert, so a company diff has both sides in hand at the same point
  the credit diff does.
- **Person resolution** (D-20 to D-25, `news.story_person`, `/admin/resolution`): the pattern
  EF-12 copies for studios and franchises.
- **D-5's short-circuit** and **D-6's promotion**: a story that names an attachment before TMDB
  has it publishes immediately and stamps the later catalog change as already carded; a story
  that arrives after the catalog card upgrades it in place. Both stand and are what makes
  "first association" a single card whatever order the sources arrive in.
- **`GET /me/follows` names** (NEU-1396): the API already returns `name` and `image_path`; only
  the frontend never adopted them.
- **`GET /people/{ref}`** (NEU-1418) and the person page (NEU-1419): the template for the
  company and collection pages.
- **The notify and digest passes** (NEU-1379, NEU-1381) and `deliverable_events()`: EF-7 changes
  which events each pass selects per follow type, not how they queue or send.
- **Import jobs** (NEU-1356, NEU-1357, `ingest/imports/`): the job row, runner, progress and
  the onboarding `ImportStep` and `ImportProgress` components. EF-22 adds a status and a review
  step in front of the follow writes.

## 5. Decisions (from the interview)

Numbered `EF-n` so tickets can cite them. Each names the accepted decision it contradicts.

### The follow

- **EF-1 A follow is binary.** `app.follow.coverage` is dropped, together with the
  `lead|major|any` control, the `422 coverage_not_applicable` rule and every tier badge.
  `POST /me/follows` takes `(entity_type, entity_id)` and nothing else. *Supersedes D-43,
  D-47, D-48.* Migration: drop the column; **delete every person follow whose `source` is
  `letterboxd_import` or `tmdb_import`** — they were written by the ratings and favorites path
  (EF-20) and never chosen, and under this model each would push on every credit change of
  someone the user once rated. Manual follows and every title follow are kept.
- **EF-2 A person follow reaches any credit.** The **recorded grade** becomes seed grade plus
  every credit of *every* followed person (`people_followed_at_any()` becomes
  `followed_people()`, still with no entitlement filter). The sweep's `followed` tranche
  enumerates every followed person. `SWEEP_ADMIT_FOLLOWED` is flipped on in Coolify as a
  deploy step of the ticket that lands this, not left as an owed action. *Amends D-49, D-50.*
- **EF-3 What a non-film follow delivers.** A person, studio or franchise follow puts on the
  timeline, in the digest and on push and email **only** the cards in which that entity is
  attached to or detached from a film — `casting`, `crew_attached`, `credit_removed` for
  people; the four EF-5 types for studios and franchises — plus the film's `canceled` card
  (EF-6). No other beat on any film reaches the user through that follow: if they want the
  film's trailers, dates and availability they follow the film. `followed_film_ids` (D-11) is
  reduced to title follows; the entity term becomes an *event* selector (an event whose
  `subject_key` or resolved story mention names the followed entity in an attach or detach
  role). *Supersedes D-11's film-coverage half, D-42's "every follow alerts", D-46 (no alert
  window applies to entity follows any more).*
- **EF-4 Admission is an attachment.** When a film's credits, companies or collection are
  observed for the first time (`credits_observed_at` NULL → set), any credit, company row or
  collection held by an entity **somebody follows at that moment** is recorded as an `added`
  change and goes through quarantine, burst grouping and sanity holds like any other
  attachment. Everything else on the new film stays baseline. Both sides of the diff read the
  same followed set, as D-49 already requires, so a follow created *after* admission never
  fabricates an `added`. *Exception to ADR-0014's "first observation is a baseline, never a
  change" (spec §5.3), for followed entities only.* This is the case that matters most: a
  director's next project being announced is why anyone follows a director, and it enters the
  catalog with the director already on it.
- **EF-5 Studios and franchises are observed.** New `catalog.film_company_change (film_id,
  company_id, change ∈ added|removed, changed_at, carded_by_event_id)`, diffed in
  `_rebuild_joins` with the same first-observation rule as credits (a
  `film.companies_observed_at` bookkeeping column, denylisted, exactly like
  `credits_observed_at`; EF-4 applies). Collection membership is read from the existing
  `film_field_change` rows for `collection_id` (`TRACKED_FIELDS` gains it), so a collection
  attachment is `NULL → id`, a detachment `id → NULL`, and a move `id → id'` is one detach and
  one attach. Four new event types, `company_attached`, `company_removed`,
  `collection_attached`, `collection_removed`, registered everywhere the vocabulary is
  enumerated (`ck_event_type`, `_EVENT_STAGE`, `_STALE_EVENT_TYPES`, the deterministic
  summary templates, the digest labels, the frontend event-type labels). Same 72-hour
  quarantine (D-3), one card per (film, event_type) per pass naming every entity (D-7), same
  `rumored` confidence as a credit attachment and the same supersession of the attach card by
  the detach card (D-2). `subject_key` carries `company:<id>` / `collection:<id>` tokens the
  way credit cards carry people. *New; D-10 named these follow types but nothing observed
  them.*
- **EF-6 Cancellation is a card.** `STATUS_EVENT_TYPES` gains `Canceled → canceled`, a
  `confirmed` catalog-sourced event (ADR-0002 makes TMDB the record for its own scalar
  fields), at most one per film, registered like EF-5's types. It is delivered to the film's
  title followers and to every follower of an entity attached to the film — any credit at any
  grade, any company, the collection — and it fires **no detach cards**: TMDB rarely strips
  credits from a cancelled title, and a run of "no longer attached" cards for a dead film
  would mislead. `Canceled` stays terminal: `ALERT_WINDOW_DEAD_STATUSES` and `in_play_clause`
  already exclude it, so nothing further cards. *Amends `catalog_events.py`'s "no event type
  in scope" comment; the `Released` transition stays uncarded.*

### Delivery

- **EF-7 The push whitelist is per follow type.** `notify_service` decides per `(user, event)`
  from *why* the event reaches the user:
  - via a **title follow**: `release_date`, `trailer`, `now_available` (per
    `user_settings.alert_stores`, D-44 stands), `canceled`, and `casting` /
    `crew_attached` / `credit_removed` where the card's people include at least one
    **seed-grade** role on that film (director, Writer/Screenplay, top-5 billed). A 12th-billed
    addition reaches the timeline and digest, not push.
  - via an **entity follow**: the attach or detach card naming that entity, and `canceled`.
  - The digest (D-33) carries everything the timeline carries for that user; no per-type
    narrowing.
  *Supersedes D-31's "alerts only for watchlist items on push-whitelist beats" and D-32's
  closed list.* Company and franchise follows and title follows can both reach the same card;
  one notification row per `(user, event, channel)` as today.
- **EF-8 Quarantine is the confirmation for a catalog attachment.** Credit, company and
  collection attach and detach cards from the catalog are `rumored` (any TMDB editor can add a
  credit) but are published only after D-3's window, so for the push decision a
  `provenance = catalog` attach or detach card counts as confirmed by construction. A
  `rumored` card with a **story** behind it — "in talks", "circling" — is the one that waits:
  see EF-10. *Refines D-32's "nothing unconfirmed pushes": the rule keys on provenance for the
  attach and detach types, and on `confidence` for everything else.*
- **EF-9 Title-follow pushes on cast and crew stop at seed grade** (the roles the credit
  history records for every film). Widening to every credit on every followed film would grow
  the recorded grade from "followed people" to "followed people plus every credit on followed
  films"; it is noted as a follow-up, not done here.
- **EF-10 Rumored associations wait.** A story-sourced attach or detach card at `rumored`
  reaches the timeline and the digest only. The push arrives when the association confirms —
  a confirmed trade story clustering onto the card (D-6) or the catalog change clearing
  quarantine (which D-5 stamps as carded by the story's card, so the card is upgraded in
  place rather than duplicated). Either way one push per attachment.
- **EF-11 A confirmed trade-story attachment pushes immediately** on the story card (D-5's
  short-circuit as it stands), and D-5's condition stands with it: the mention must have
  **resolved** to the followed entity, so an `unlinked` or `not_in_tmdb` mention never alerts
  (D-25).

### News

- **EF-12 Studios and franchises resolve.** D-20's extraction schema grows organisation
  mentions: `(name_as_written, kind ∈ company|collection, title_mentioned, event_type,
  evidence_span)`. Candidates: `/search/company` or `/search/collection` on the name, plus the
  linked film's current companies or collection; scoring reuses the deterministic feature
  shape (name match, already attached, popularity tiebreak); the same `resolve` gateway stage
  (D-22) takes the narrow band. Storage: `news.story_entity (story_id, kind, entity_id NULL,
  name_as_written, evidence_span, confidence, path ∈ accepted|tiebreak|unlinked|not_in_tmdb,
  features, candidates, resolved_at)` beside `story_person` (people keep their table and its
  person-specific features). `/admin/resolution` lists all three kinds. INV-5, INV-6 and INV-8
  apply unchanged. *Extends D-20 to D-25.*
- **EF-13 A story mention counts only as the first association or the first detachment.**
  A resolved mention (path `accepted` or `tiebreak`) of a followed entity reaches that
  entity's followers when its extracted `event_type` is an attach type and the entity has no
  published attach card and no current credit, company row or collection on that film, or
  when it is a detach type and the entity has no published detach card for that film since
  its last attachment. Every other mention — an interview, a festival piece, a second outlet
  reporting the same casting — produces nothing for the entity follower (the second outlet
  attaches to the existing event, per **Attach** in the glossary, and does not re-alert).
  *Supersedes D-11's "events that name a resolved person also match"
  (`events_naming_followed_people`).*

### Surfaces

- **EF-14 The watchlist is gone.** `app.watchlist_dismissal` is dropped; `GET`, `POST` and
  `DELETE /me/watchlist` are removed; `watchlist_film_ids` / `covered_film_ids` /
  `covering_follows` / `covered_by_any_user_clause` collapse to **title-follow** builders
  (`title_follow_film_ids(user_id)`, `title_followed_by_any_user_clause()`). The watchlist
  calendar (`GET /me/calendar`, the `.ics` feed), the digest slate and D-27's poll-set rule
  ("plus any film with a follow") all read title follows. The frontend deletes the
  `/me/watchlist` route, page, Muted section and `useToggleWatchlist`; the film page's
  `TitleFollowButton` toggles a title follow directly. The calendar's first tab reads **"My
  films"**. *Supersedes D-45, ADR-0018's "two pages stay two pages".*
- **EF-15 The follows page is one list.** Flat, every type together, with **type chips**
  (Films / People / Studios / Franchises, multi-select, none selected = all), **sorts** (name
  A–Z, newest followed, last activity) and a client-side text filter over names. `GET
  /me/follows` rows carry `name`, `image_path` (already), `created_at` (already) and new
  `last_activity_at` — the `created_at` of the latest published card that would reach this
  user through this follow (EF-3 for entities, any beat for titles), NULL when none. Person,
  studio and franchise rows link to their pages; film rows to the film page. The frontend
  `Follow` type adopts `name` and `image_path` and **`lib/follow-labels.ts` is deleted** (the
  NEU-1396 follow-up, shipped as NEU-1422 on 2026-09-20). *Replaces NEU-1415's grouped page
  and coverage radio.*
- **EF-16 The film page has one button** — the film's — and every person, studio and
  franchise name links to its page. The seed-row buttons NEU-1419 kept are removed, and the
  "via …" line with them. Blocked on EF-17 so no type is stranded.
- **EF-17 Every entity has a page, and the header finds it** (NEU-1420, moved into this
  project). `GET /companies/{ref}` and `GET /collections/{ref}` shaped like `GET /people/{ref}`
  (`<id>-<slug>`, canonical-ref redirect, 404 for unknown), each with `upcoming` (in play) and
  `recent` (inside the alert window, not in play) film lists; frontend `/studio/:ref` and
  `/franchise/:ref` in the public layout. The header `SearchBox` searches all four types in
  one grouped dropdown (films, people, studios, franchises; one request per type, no
  cross-type ranking). Sitemap: person, studio and franchise pages join it.
- **EF-18 An entity page shows what a follow delivers.** Beside the follow button and the
  film list (no tier badges), each entity page lists the entity's own recent attach, detach
  and `canceled` cards, newest first, from `GET /people/{ref}/events`,
  `/companies/{ref}/events`, `/collections/{ref}/events` (page-sized, same `EventOut`, same
  visibility terms as the feed).
- **EF-19 Vocabulary on screen:** a `company` is a **Studio**, a `franchise` (a TMDB
  collection) is a **Franchise**, a `title` is a **Film**. Code keeps `company` / `franchise`
  / `title`.

### Imports

- **EF-20 Ratings and favorites contribute nothing.** The people treatment in
  `ingest/imports/apply.py` is deleted; `ratings.csv` is ignored and the TMDB import reads the
  account watchlist only. *Supersedes NEU-1356 §3's ratings row and NEU-1357's favorites
  row.*
- **EF-21 Only films inside the alert window are candidates:** any status but `Canceled`,
  primary release date in the future or within `PROVIDER_POLL_MAX_AGE_DAYS` (365) of today
  (D-46's window, `alert_window_clause`). A matched film outside it is listed as skipped with
  reason `outside_window`; an unmatched title as today.
- **EF-22 The import is two-phase.** The job resolves titles and upserts films as today, then
  stops at a new status **`awaiting_review`** with its candidates in `app.import_candidate
  (job_id, film_id, tmdb_id, title, headline_release, selected DEFAULT true, skip_reason
  NULL)`. `GET /me/imports/{id}` returns them; `POST /me/imports/{id}/confirm {film_ids}`
  writes one title follow per selected film and moves the job to `succeeded`
  (`follows_created` counts the confirmed rows). Starting a new import discards an unconfirmed
  one. The onboarding `ImportStep` renders the review list with every in-window row ticked and
  the skipped rows greyed with their reason; Confirm creates the follows.

## 6. Milestones and shared contracts

Order is deploy order. M2 lands the signals before M3 removes the coverage that delivers
today's cards, so no subscriber has a silent interval; during the M2→M3 gap an entity follow
behaves as the old `any` tier.

### M1 — Every entity has a page

**Goal:** a subscriber can find any person, studio, franchise or film from the header and land
on a page that holds its follow button, and every follows-page row shows a real name and
links inward (EF-15's name adoption, EF-17). NEU-1420 moves here. Ships before the film page
loses its buttons.

**Shared contracts**
- `GET /companies/{ref}` and `GET /collections/{ref}` return `{ref, id, name, logo_path |
  poster_path, upcoming: [FilmRow], recent: [FilmRow]}` where `FilmRow` is `GET
  /people/{ref}`'s film row without `credits` and `tier`. Same ref rules as people.
- `/companies/search` and `/collections/search` stand; the header calls the four search
  endpoints with one debounced query and renders four groups.
- Frontend routes: `/studio/:ref`, `/franchise/:ref`; `pages/MyFollows.tsx::tmdbUrl` deleted.
- Frontend `Follow` type carries `name: string | null`, `image_path: string | null` and
  `lib/follow-labels.ts` is gone — done by NEU-1422 (frontend #160, 2026-09-20) before this
  project started; NEU-1431 only adds the studio and franchise links.

### M2 — The signals

**Goal:** the catalog observes every attachment and detachment this project will deliver:
binary follows, every followed person at any credit, studio and franchise membership, film
admission, and cancellation (EF-1, EF-2, EF-4, EF-5, EF-6). Backend only. Nothing about
delivery changes yet.

**Shared contracts**
- Migration: drop `app.follow.coverage`; delete imported person follows; `FollowOut`,
  `POST /me/follows` and `PATCH /me/follows` lose `coverage` (the frontend control is removed
  in M3; until then the API ignores the field rather than 422-ing it).
- `catalog.film_company_change` and `film.companies_observed_at`; `TRACKED_FIELDS` gains
  `collection_id`; event types `company_attached`, `company_removed`, `collection_attached`,
  `collection_removed`, `canceled` registered in every enumeration listed in EF-5, with
  deterministic summaries ("Legendary Pictures joins *Dune: Part Three*", "*Dune: Part Three*
  has been cancelled").
- `subject_key` tokens `company:<tmdb_id>`, `collection:<tmdb_id>`; people tokens unchanged.
- `followed_people()` replaces `people_followed_at_any()`; the recorded grade reads it.
  `followed_companies()` and `followed_franchises()` join it — same shape, no user and no
  entitlement filter — and are read only by EF-4's admission exception (NEU-1436, D-1436.5).
- `film.companies_observed_at` is **backfilled to `now()` on every existing film** by its own
  migration (NEU-1433), and `film.credits_observed_at` by NEU-1436's. EF-4 keys "is this a
  first observation?" on those markers, so a film left unstamped would read as newly admitted
  on its next refresh and card an attachment for every followed entity on it (D-1436.3,
  D-1436.6).
- The one exception to "first observation is a baseline" is EF-4's, and the franchise half of
  it writes a synthetic `film_field_change` row (`collection_id`, `NULL → id`) from the
  admission path, because the trigger is `BEFORE UPDATE` (D-1436.4).
- Deploy: flip `SWEEP_ADMIT_FOLLOWED=true` in Coolify with this milestone's sweep ticket and
  verify `followed×N` on `/admin/runs`. **At least one sweep must complete before NEU-1436
  deploys**, so the followed backlog enters as baselines rather than as a day-one flood of
  cards (D-1436.7).

### M3 — The cutover

**Goal:** an entity follow delivers its attachment stream and nothing else; a title follow
delivers everything; the watchlist, mutes and tiers are gone from every surface (EF-3, EF-7,
EF-8, EF-9, EF-13 for people, EF-14, EF-15, EF-16, EF-18). Backend then frontend.

**Shared contracts**
- `app/follow_queries.py`: `title_follow_film_ids(user_id)`,
  `entity_attachment_event_ids(user_id)` (events whose `subject_key` tokens or resolved
  mentions name a followed entity in an attach or detach role, or `canceled` on a film the
  entity is attached to), `title_followed_by_any_user_clause()`. The timeline is
  `film_id IN title_follow_film_ids OR id IN entity_attachment_event_ids`. A followed person is
  matched to a card by normalized name (`subject_key` carries names, not ids); companies and
  collections by their id tokens. EF-13's predicate is `first_association_clause(user_id)`, one
  builder both the timeline and the notify pass reach, with an attach arm and a (still empty at
  M3) detach arm; it carves out a credit stamped `carded_by_event_id = the card` so a D-5 story
  card survives its own confirmation (NEU-1437 spec).
- `deliverable_events()` drops its `confidence = 'confirmed'` term: the digest carries rumored
  cards as the timeline does (EF-7). The alert branch carries `confirmed` as the interim rule
  until NEU-1438 applies EF-8's provenance clause. NEU-1437 rewires the event readers
  (timeline, digest, alert); the film readers (calendar, iCal, slate, poll set) and the
  watchlist builders and mutes are NEU-1439's.
- `notify_service`: `PUSH_WHITELIST` becomes two sets, `TITLE_PUSH_TYPES` and
  `ENTITY_PUSH_TYPES`, applied per reach; the seed-grade clause for title-follow credit
  cards reads `seed_grade.is_seed_role`; the provenance clause of EF-8.
- `GET /me/follows` rows gain `last_activity_at`; `GET /me/watchlist`, `POST`, `DELETE`
  removed; `GET /me/calendar` and the `.ics` feed read title follows; D-27's poll set reads
  `title_followed_by_any_user_clause()`.
- `GET /people/{ref}/events`, `/companies/{ref}/events`, `/collections/{ref}/events`:
  `{items: [EventOut], next_cursor}`.
- Frontend: `/me/watchlist` route, page, `useToggleWatchlist`, `coveredBeyondTitleFollow`,
  `CoverageControl`, `FollowCoverageControl`, tier badges deleted; film page
  `TitleFollowButton` posts/deletes a title follow; `MyFollows.tsx` rebuilt per EF-15; entity
  pages gain the cards section; calendar tab label "My films".

### M4 — Studios and franchises in the news

**Goal:** a story that names a studio or franchise joining or leaving a film reaches that
entity's followers once, and the admin queue shows the decision (EF-12, EF-13 for all
kinds).

**Shared contracts**
- Cluster output schema: `mentions` gains `organisations: [{name_as_written, kind,
  title_mentioned, event_type, evidence_span}]`; the resolve stage's shortlist prompt takes
  either kind.
- `news.story_entity` per EF-12; `RESOLVED_MENTION_PATHS` shared with `story_person`.
- `/admin/resolution` (backend DTO and frontend page) lists `kind ∈ person|company|collection`
  with one filter.
- `entity_attachment_event_ids` reads `story_entity` beside `story_person`, under EF-13's
  first-association predicate, which lives in one query builder used by both the timeline and
  the notify pass.
- The cluster vocabulary gains `company_attached`, `company_removed`, `collection_attached`
  and `collection_removed`; an organisation mention's `event_type` uses the same four values,
  so a card type and a mention type are one word per beat.
- `catalog.film_field_change` gains `carded_by_event_id` — one stamp shape for all three
  kinds. Only its `collection_id` rows ever carry one, and a *move* (`id -> id'`) is never
  stamped: one row, two beats, one stamp column.
- A sweep step at the end of the pass flips a stamped story card from `rumored` to
  `confirmed` once its change has cleared quarantine and live state still agrees, bumping
  `updated_at` — the writer EF-10's push window reads.

### M5 — Imports you review

**Goal:** an import creates title follows only, only inside the alert window, and only after
the user confirms the list (EF-20, EF-21, EF-22).

**Shared contracts**
- `ImportJobOut.status` gains `awaiting_review`; `candidates: [{film_id, tmdb_id, title,
  headline_release, selected, skip_reason}]` on `GET /me/imports/{id}` in that status;
  `POST /me/imports/{id}/confirm {film_ids: [uuid]}` → 200 with the finished job, 409 unless
  the job is `awaiting_review`.
- `ACTIVE_IMPORT_STATUSES` gains `awaiting_review` for the one-active-import rule; starting a
  new import discards an `awaiting_review` job.
- Frontend `ImportProgress` gains the review list; `ImportStep`'s copy stops mentioning
  ratings.

### Ticket map (Linear, created 2026-09-21)

Project `bl: Entity Follows` — https://linear.app/neuroticsasquatch/project/bl-entity-follows-558b3435c60e

| Milestone | Story | Tickets (blocked by) |
|---|---|---|
| M1 | NEU-1420 | NEU-1428 backend company/collection endpoints; NEU-1429 frontend studio/franchise pages (1428); NEU-1430 frontend header search; NEU-1431 frontend studio/franchise links on follows rows (1429; names already shipped by NEU-1422) |
| M2 | NEU-1423 | NEU-1432 binary follow + recorded grade + sweep flip; NEU-1433 company change tracking + events; NEU-1434 collection field events; NEU-1435 `canceled`; NEU-1436 admission-as-attachment (1432, 1433, 1434) |
| M3 | NEU-1424 (backend) | NEU-1437 timeline/digest clause + first association for people (1432–1435); NEU-1438 per-type push whitelist (1437); NEU-1439 remove the watchlist (1437); NEU-1440 `last_activity_at` + entity `/events` (1437) |
| M3 | NEU-1425 (frontend) | NEU-1441 film page one button (1429, 1439); NEU-1442 follows page rebuilt (1431, 1440); NEU-1443 delete watchlist page + calendar tab (1439, 1441); NEU-1444 entity page cards, tier control gone (1429, 1440) |
| M4 | NEU-1426 | NEU-1445 organisation extraction + resolution + `story_entity`; NEU-1446 first-association builder for all kinds (1445, 1438); NEU-1447 frontend admin resolution kinds (1445) |
| M5 | NEU-1427 | NEU-1448 drop ratings path + window filter; NEU-1449 two-phase job + confirm (1448); NEU-1450 frontend review list (1449) |

## 7. Prerequisites and deploy notes

- Backend before frontend in every milestone; M2 before M3 in production (see §6).
- `SWEEP_ADMIT_FOLLOWED=true` in Coolify with M2's sweep ticket (compose fallbacks are
  shadowed — AGENTS.md gotcha); `printenv` on the running container to confirm.
- The other 2026-09-20 session finishes NEU-1416 (M9 of Consumer Pivot); NEU-1420 is moved
  out of M9 into this project's M1 and re-described.
- Everything runs in the container via `task`; before claiming any ticket done: `task format`,
  then `task test && task lint && task typecheck` in the repo touched.

## 8. Open items (not blocking)

- Widening the recorded grade to every credit on followed films (EF-9's follow-up).
- Whether `company_attached` should be `confirmed` rather than `rumored` once quarantine has
  cleared; measure how often TMDB reverts a company row before deciding.
- Expiring `awaiting_review` jobs on a clock rather than on the next import.
