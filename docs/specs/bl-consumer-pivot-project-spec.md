# bl: Consumer Pivot — project spec

**Status:** approved design, scaffolded in Linear by `/personal:projectit` (2026-09-15)
**Source:** `docs/specs/backlotter-consumer-pivot-spec.md` (the product/design spec — read its §2
invariants before writing any code; they are restated here only where a decision below refines
them)
**Repos:** `upcoming-movies-backend` (primary), `upcoming-movies-frontend`
**Glossary:** `CONTEXT.md` — sections *Claims and publication*, *Person resolution*, *Follows,
watchlist and delivery* were added for this project. Use those terms.
**ADR:** `docs/adr/0017-the-claim-ledger-is-the-event-table.md`

> **Superseded in part (2026-09-21).** D-42 to D-50, and D-11's film-coverage half, D-13, D-45 and
> the follow-related parts of D-27, D-31 and D-32, are superseded by *bl: Entity Follows*
> (`docs/specs/bl-entity-follows-project-spec.md`, ADR-0019): a person, studio or franchise follow
> delivers that entity's attach and detach cards only; the watchlist, mutes and coverage tiers are
> removed. NEU-1420 moved to that project. Everything else here stands.

This document is the project-wide spec that `/personal:implementit` falls back to for any
ticket in the project (ADR-0004 of the personal plugin). It records the outcome of the
project-shaping interview: every decision that the source spec left open or that the existing
codebase forced, plus the milestone contracts. Where it is silent, the source spec governs.

---

## 1. Purpose

backlotter pivots from a production-news tracker for industry professionals to a **follow-based
film tracker for consumers**: users follow people, companies, franchises and titles and receive a
personalized timeline covering a film's whole public lifecycle — announcement → casting →
production → theatrical date → home release. The defining property (MusicHarbor for movies) is
that the product tells you about a film you had *never heard of*, because you follow the people
who made it. Home-release alerting is table stakes for retention, not the positioning; the
differentiator is the follow graph auto-seeding the watchlist plus forward-looking announced
digital dates, which JustWatch and Letterboxd cannot surface.

## 2. Scope

In scope (seven milestones, §6):

1. Accounts and the delivery pipe: transactional email, verification, open signup, rate limits
   (absorbed from the Backlog projects *bl: Transactional Email* and *bl: Open Signup & Abuse
   Controls*, both cancelled in favour of this project).
2. The claim ledger: `news.event` gains supersession; the event/state split is enforced and made
   visible (confidence on every card, superseded marker).
3. The follow graph, personalized timeline, Letterboxd and TMDB imports, onboarding.
4. Person resolution: story mention → `catalog.person`, with an unlinked queue and a cache.
5. Quarantine for credit attachments, the publication queue, burst collapsing, Tier-A
   short-circuit, promotion.
6. Home release: US digital/physical dates in the displayable set, watch-provider polling,
   `now_available` events, where-to-watch on the film page.
7. Delivery: watchlist alerts by email, digests, weekly slate, iCal, trailers, Web Push last.

Out of scope for this project (explicit non-goals, from the source spec §1.4 plus interview):

- Pricing, payment and subscription lifecycle for the consumer tier (*bl: Subscription &
  Billing* stays its own project; the prior $4/mo figure is not assumed). This project decides
  *where* access is enforced and how it is granted by hand (§5 *Access and entitlement*); it does
  not decide who pays or how much. Billing later takes over writing `app.user.entitled_until`.
- Service-to-service availability churn and leaving-service alerts.
- Industry features: slate tracking, competitive intelligence, deal flow.
- A native mobile app. v1 is web + email + Web Push + iCal.
- A manual-correction UI for resolution (read-only admin list only, §5.4).
- A hand-curated franchise mapping (franchise = TMDB collection, §5.3).

## 3. Goals and acceptance (project level)

- A new user *who has been granted access* can go from signup to a populated timeline via one
  Letterboxd CSV upload.
- A registered user who has **not** been granted access can reach nothing a signed-out visitor
  cannot: no follows, no timeline, no imports, no alerts, no digest, no iCal — and the notify and
  digest passes never mail them (D-37, D-39).
- A credit added and reverted inside the quarantine window produces zero events and a correct
  live cast list throughout; added and removed weeks apart produces two events, the first marked
  superseded by the second.
- No published event is ever retracted silently; no released-from-quarantine event ever sorts
  below a user's read watermark (publication axis, ADR-0016/0017).
- Every resolution decision is individually inspectable: candidates, feature scores, path.
- A title moving between flatrate services after first availability produces zero events.
- Following a prolific actor produces timeline rows only; pushes come from the watchlist alone.

## 4. What already exists (do not rebuild)

Facts established against the codebase on 2026-09-15; the tickets assume them.

| Capability | Where | Implication |
|---|---|---|
| Live state mirror of cast/crew | `catalog.film_credit` — delete-and-rebuild per ingest | INV-1 state half is done. Never hold it. |
| Raw observation log | `catalog.film_credit_change`, `film_release_date_change`, `film_field_change` | These are the "detected" layer; `changed_at` = first detection. |
| Event ledger | `news.event` with `provenance`, `confidence`, `occurred_at`, `created_at`, `subject_key` | Becomes the claim ledger (ADR-0017); add `status`, `superseded_by`. |
| Publication axis | ADR-0016: feed groups by `created_at`; film page by `occurred_at` (NEU-1204) | INV-2 already holds. Notifications key on `created_at`. |
| Removal quarantine | NEU-1205 forward-dwell gate (`SWEEP_CREDIT_DWELL_DAYS`=3) in `ingest/sweep/credit_events.py` | Attachment quarantine mirrors this shape. |
| Correction cards | NEU-1200 `credit_removed`, gated on a prior visible attachment card | Supersession links the pair; nothing is hidden. |
| Promotion in place | ADR-0014: a story clustering onto a catalog event upgrades the card; double-carding rules by `subject_key` | INV-4 short-circuit and "promotion not duplication" build on this. |
| Film page layout | `routes/film.tsx`: header → release dates → plot → cast → crew → companies → event log | The §4.1 "cast/crew on top" restructure is already the layout; what remains is confidence styling and the superseded marker. |
| Release dates incl. types 4/5 | `catalog.film_release_date` stores all types; `catalog/release_grade.py` restricts the displayable set to US/origin theatrical (2,3) | Home-release dates enter by widening that one module. |
| Collections, companies | `catalog.collection` via `belongs_to_collection`; `film_production_company` | Franchise and company follows need no new ingest. |
| People | `catalog.person` (TMDB id PK, popularity, profile_path); no aliases table | Onboarding grid and follow targets come from here. |
| LLM gateway | Closed stage set `link, cluster, source_judge, summarize`; per-stage provider; never falls back | `resolve` is a new stage; extraction extends cluster output. |
| Cluster output | Emits `subject_key` as normalized names (string, not id) | Extraction tuples extend this schema. |
| Auth | Cookie sessions, argon2, invite-gated signup, login throttling only; **no email, no user settings, no follows/watchlist/notifications, no rate limiting** | Milestone 1 builds the pipe. |
| Admin surface | `app.user.is_admin` + `require_current_admin` (`deps.py`), session-authed, distinct from the `ADMIN_TOKEN` `require_admin`; `/admin/invites` router + page is the write-surface precedent | The entitlement grant surface (D-38) mirrors `invites_admin`, session-authed — not the `ADMIN_TOKEN` routers. |
| Frontend | React 19, react-router v8 framework mode (SSR on Cloudflare Workers), TanStack Query, Tailwind 4, shadcn | Routes: `/`, `/calendar`, `/film/:ref`, `/login`, `/signup`, `/admin/*`. |
| TMDB client | `/discover/movie`, `/movie/{id}?append_to_response=credits,release_dates,alternative_titles`, `/person/{id}/movie_credits`; 40 req/10 s limiter | New endpoints: `/search/person`, `/search/movie`, `/movie/{id}/watch/providers`, `/movie/{id}/videos`, `/account/*` + auth flow. |

## 5. Decisions (from the interview)

Numbered so tickets can cite them (`D-n`).

### Claims, quarantine, publication

- **D-1 Claim ledger = `news.event`.** No new claim table. Add `status` (`published` |
  `superseded`) and `superseded_by` (FK to `news.event`). Raw observations stay in the catalog
  change tables and `news.story`. ADR-0017.
- **D-2 Supersession marks the original.** When a `credit_removed` card publishes for a person,
  their published attachment card (`crew_attached`/`casting`, any provenance, `occurred_at` before
  the removal) is set `superseded`, `superseded_by` = the removal card. Both stay on every
  surface; the original renders a "later retracted" marker linking to the correction. Never
  hidden, never deleted. Feed and film page both show superseded cards in place.
- **D-3 Attachment quarantine = hold before carding.** A seed-grade credit attachment cards only
  once `changed_at + SWEEP_CREDIT_QUARANTINE_HOURS ≤ now` **and** the credit is still present in
  `film_credit`. Default **72h**, setting-driven, 0 disables. A credit reverted inside the window
  never becomes an event. No `pending` rows exist. `created_at` is therefore publication by
  construction.
- **D-4 Window tuning is a spike, not a wait.** `film_credit_change` has logged every add/remove
  since 2026-08. A spike ticket plots survival-time-to-deletion, recommends the knee, and
  recommends whether a variable bar (by billing order, department, defacement-prone titles) is
  worth it. v1 ships a uniform window over seed-grade credits (director, writer, top-5 cast — the
  only credits that card today, so "low-billing cast" already never cards).
- **D-5 Tier-A short-circuit (INV-4).** Tier-A = a **trade feed** story (all eight curated
  feeds). `film_credit_change.carded_by_event_id` records which event published a change: cluster
  stamps in-window pending changes named by a new story card, and the sweep loader stamps a
  pending change that finds an earlier story card; stamped rows are never carded by the sweep.
  Requires the person to have resolved (M4) for *alerts*; the feed card itself does not
  require resolution. Spec: `NEU-1371-tier-a-short-circuit.md`.
- **D-6 Promotion, not duplication.** Unchanged from ADR-0014: a story clustering onto a
  published catalog card upgrades that card in place (`news_backed` flips). Preserve
  `occurred_at` so "we had it first" is provable.
- **D-7 Burst collapsing.** Attachments for one film that clear quarantine in the same sweep pass
  publish as **one card per (film, event_type)** for that pass, naming all people, with
  `occurred_at` = the latest `changed_at` in the group. The `uq_event_catalog_change` constraint
  still holds because the group has one `occurred_at`. Intra-day ordering by significance reuses
  `public/arc.py::_EVENT_STAGE` ranking (billing order breaks ties within `casting`).
- **D-8 Sanity checks** run in the sweep's credits phase and *hold* rather than discard: a person
  attached to ≥20 films in one observation day; a credit inconsistent with birth/death dates
  (fetched lazily from `/person/{id}` only for people about to be carded); an add→remove→re-add
  flap inside the window (already suppressed by D-3 + NEU-1205). Held items are logged with a
  reason and re-evaluated next pass. Spec: `NEU-1370-sanity-holds.md`.
- **D-9 Confidence is visible.** Every card on the feed, timeline and film page renders its
  `confidence` (`confirmed` / `unconfirmed`) as a badge; the "unconfirmed updates" section heading
  stays. [2026-09-20, NEU-1406: the heading clause is superseded — the section is renamed
  "Not yet reported", because "unconfirmed" collided with this badge; the badge clause stands.]
  Superseded cards render the D-2 marker. Backend `EventOut` exposes `confidence`,
  `status`, `superseded_by`, and `occurred_at` (the ADR-0016 residual: disclose it on the card
  as a small "first seen <date>" line on the film page).

### Follows, timeline, watchlist

- **D-10 Follow entities:** `person` (TMDB person id), `company` (TMDB company id), `franchise`
  (= `catalog.collection` id, v1), `title` (film id). `source` ∈ `manual | letterboxd_import |
  tmdb_import | derived`.
- **D-11 Person follow coverage (pre-resolution)** *(amended 2026-09-20 by D-47: a follow at
  coverage `any` reaches every credit, not only seed grade)*: every published event on any **in-play**
  film where the person holds a **seed-grade** credit (`catalog/seed_grade.py`: director,
  Writer/Screenplay, top-5 billed). After M4, events that *name* a resolved person on a film
  they are not yet credited on also match. Company follow = films with that
  `film_production_company`; franchise = `film.collection_id`; title = the film.
- **D-12 Timeline lives at `/`.** Signed-in users see the timeline (the grouped feed filtered by
  their follows, same DTO shape and day grouping); anonymous visitors see the global feed at
  `/`; the global feed is also served at `/feed` for everyone. `/` always server-renders the
  global feed; a client island swaps to the timeline once `me` resolves (the SSR-never-resolves-
  auth invariant stands; spec `NEU-1354-home-timeline-swap.md`). Supersedes *bl: Home Page & Feed
  Split* (cancelled). Empty follow graph renders an onboarding prompt, not an empty page.
- **D-13 Derived watchlist rule** *(superseded 2026-09-20 by D-42 to D-45; the cut survives as
  `coverage=lead`)*. A film is auto-added (`source=derived_from_follow`) when it is
  in play and: a followed person is its **director or in its top-3 billing**, or a followed
  company / franchise / title matches. Writers and cast 4–5 feed the timeline only. Derivation
  runs on follow creation and on every sweep credits pass. A user's removal of a derived item
  writes a **dismissal** row `(user, film)` that blocks re-derivation permanently.
- **D-14 Watchlist prefs** *(superseded 2026-09-20 by D-44: one store set per user)*:
  `alert_prefs` is a subset of `{buy, rent, stream}` per item, default
  `{stream}`; plus the push-whitelist beats (date assigned/moved, home-release date, trailer)
  which are always on for a watchlist item.

### Imports and onboarding

- **D-15 Letterboxd CSV import.** Accepts the export zip or the individual `watchlist.csv` and
  `ratings.csv`. Titles resolve to TMDB via `/search/movie` with year (exact-title-and-year first,
  then normalized title; unmatched rows are reported back to the user, never guessed). Watchlist
  rows → title follows + watchlist items (`source=letterboxd_import`). Ratings **≥ 4.0** →
  person follows for the film's **director and top-2 billed cast** (from TMDB credits of the
  matched film; persons upserted into `catalog.person` if absent). De-duplicated; idempotent on
  re-upload. Runs as a **background job** with a pollable `app.import_job` row (202 + `GET
  /me/import/{id}`); rated films contribute **people only**, watchlist films are upserted in
  full. Spec: `NEU-1356-letterboxd-import.md`.
- **D-16 TMDB account import.** Full v3 user-auth flow (request token → user approves on
  themoviedb.org → session id). **One-shot:** the callback schedules the D-15 import job, which
  deletes the TMDB session when done; nothing is stored. Imports account watchlist and
  favorites: watchlist → title follows + watchlist items; favorites → person follows by the
  D-15 rule (`source=tmdb_import`). Last ticket of the milestone so it can slip. Spec:
  `NEU-1357-tmdb-account-import.md`.
- **D-17 Onboarding.** After signup: (1) import CTA (Letterboxd / TMDB), skippable; (2) a grid of
  ~30 `catalog.person` rows by popularity with profile photos plus a person search box, tap to
  follow; (3) land on the timeline. Re-enterable from settings.
- **D-18 Open signup.** Public signup with Cloudflare Turnstile. Email verification is sent at
  signup; an **unverified** user may browse, follow and watchlist, but receives **no** digest or
  alert until verified. Invite codes stay as an admin-only comp path, not required.
- **D-19 Rate limiting.** One per-IP limiter dependency (in-process token bucket behind a
  store protocol; the API is one process) applied to signup, login, password reset,
  verification, imports, and the public feed/search/film/calendar routes; limits are settings.
  Uvicorn runs with proxy headers on; the SSR Worker signs its fetches with a shared secret and
  forwards the visitor IP in a dedicated header (NEU-1389); the public bucket ships off and is
  enabled after the Worker change deploys. Spec: `NEU-1344-rate-limiter.md`.

### Resolution

- **D-20 Extraction extends cluster.** The cluster stage's output schema grows to emit, per
  story, tuples `(name_as_written, role, department, title_mentioned, event_type,
  evidence_span)` alongside the existing `subject_key`. The model never emits ids (INV-5).
- **D-21 Candidates and scoring are deterministic Python.** Candidate union: `/search/person`
  on the name; the linked film's current `film_credit`; anyone in that film's
  `film_credit_change` in the last 14 days. Cap 10. Features: name match quality, already
  credited / in recent change stream, department vs role, filmography overlap with other titles
  named in the article, age/alive plausibility, popularity prior (tiebreak only). Wide margin →
  accept; nothing good → unlinked; narrow band → tiebreak.
- **D-22 New gateway stage `resolve`.** Closed-set multiple choice (article text + shortlist with
  known-for credits; pick one or none). Its own `(provider, model)` config, validated at startup
  like the others. Target ≤10% of mentions.
- **D-23 Confidence propagates down (INV-6).** `story_person.confidence ≤ story.link_confidence`.
- **D-24 Storage.** `news.story_person (story_id, person_id NULL, name_as_written, role,
  department, evidence_span, confidence, path ∈ accepted|tiebreak|unlinked|not_in_tmdb,
  features JSONB, candidates JSONB, resolved_at)` and `news.resolution_cache (source_domain,
  name_as_written, film_id, person_id NULL, confidence, resolved_at)`. `person_id NULL` with
  `path=not_in_tmdb` is a valid outcome (INV-8). `Event.subject_key` keeps names; resolved ids
  attach to the story, and the event's people are the union over its stories.
- **D-25 Unlinked queue.** Log-only plus a read-only `/admin/resolution` page listing unlinked
  and tiebreak decisions with candidates and features. Unlinked mentions never match a person
  follow and never alert.

### Home release

- **D-26 Displayable set widens.** `catalog/release_grade.py` adds buckets `digital` (4) and
  `physical` (5), **US only**, alongside theatrical. Film page lists them, the calendar gains a
  home-release bucket, and a home-release date being set or moved cards a `release_date` event
  (`confirmed`, ADR-0014 refinement) with `subject_key` token `US:digital` / `US:physical`.
- **D-27 Provider polling.** `/movie/{id}/watch/providers`, region `US` in v1, schema keyed by
  `(film_id, region, provider_id, monetization_type)`. Poll set = films whose US theatrical
  governing date is 14–365 days old (200 until NEU-1417; see D-46) **plus** any film with a
  follow or watchlist item. Daily,
  in the sweep's slot, with its own `ingest_run.kind`.
- **D-28 `now_available` event.** New catalog-sourced event type, `confirmed`, one card per
  (film, monetization_type) on first insert into `availability_first_seen`; body names the
  providers observed. Insert-only; later provider changes produce nothing. Alerts per watchlist
  `alert_prefs`. JustWatch attribution renders wherever provider names render (TMDB terms).
- **D-29 Where to watch.** Film page box listing current US providers by monetization type with
  TMDB's provider link, plus JustWatch attribution.

### Delivery

- **D-30 Email provider = Resend** behind a first-party `mail/` gateway (ADR-0007 pattern);
  provider choice is configuration; SPF/DKIM/DMARC on the sending domain.
- **D-31 Notification queue.** `app.notification (user_id, event_id, kind ∈ alert|digest,
  channel ∈ email|push, status ∈ queued|sent|failed|suppressed, created_at, sent_at)`.
  Written by a decision pass over newly published events (`created_at` since last pass), never
  by ingest. Alerts only for watchlist items on push-whitelist beats; everything else queues for
  the digest. Unverified users: `suppressed`.
- **D-32 Push whitelist** *(superseded 2026-09-26 by ADR-0021: no beat interrupts; the digest is the only delivery)*: `release_date` (assigned or moved, US theatrical or home-release —
  slips flagged in copy), `now_available` (per prefs), `trailer`. Nothing `unconfirmed`.
- **D-33 Digest:** per-user `digest_cadence ∈ daily|weekly|off`, default **weekly**; the weekly
  send *is* the "your slate" mail (timeline highlights + upcoming dates for watchlist items).
- **D-34 iCal:** per-user tokenised `/calendar/{token}.ics`; one all-day VEVENT per (watchlist
  film, US governing date) over theatrical, digital, physical; stable UIDs so date moves update
  in place. Token rotatable from settings.
- **D-35 Trailers:** poll `/movie/{id}/videos` on the same scoped set as D-27; a new
  YouTube video of type `Trailer` cards a `trailer` event (type exists; `confirmed`), which is
  on the push whitelist *(removed by ADR-0021)*.
- **D-36 Web Push** *(superseded 2026-09-26 by ADR-0021: removed with the alert mail)* ships last: service worker, VAPID keys, `app.push_subscription`, same queue
  as email with `channel=push`. iOS requires home-screen install; documented, not worked around.

### Access and entitlement

The follow graph, the timeline, imports and everything in delivery are **subscriber
functionality**. This project builds them; it does not give them away. Until
*bl: Subscription & Billing* ships, the only way in is an admin grant.

Consequence, accepted deliberately: a registered-but-ungranted account confers nothing over a
signed-out visit. M1's open signup, verification and rate limits still ship — they are
prerequisites for granting access at all, and for the billing project on top — but the signup
copy must not promise the follow graph while nobody can buy it. Revisit that copy when billing
lands.

- **D-37 Entitlement is closed by default.** `app.user.entitled_until` (nullable timestamp,
  default NULL). A user is *entitled* iff `entitled_until IS NOT NULL AND entitled_until > now()`.
  NULL is the default for every signup. There is deliberately **no** global "everyone is
  entitled" setting and **no** automatic trial grant at signup: an unentitled account is the
  normal state, and access is only ever conferred per user, by hand (D-38), until the billing
  project takes over writing the column from its payment provider. Public surfaces are
  unaffected and stay public to signed-out visitors: `/feed`, `/film/:ref`, `/calendar`, search.
- **D-38 Granting is a session-authed admin action.** `app/entitlements.py` exposes
  `is_entitled(user) -> bool` and `require_entitled()` (FastAPI dependency; 403
  `entitlement_required`). Grant and revoke via `GET /admin/users`,
  `PUT /admin/users/{id}/entitlement` (body: `entitled_until`) and
  `DELETE /admin/users/{id}/entitlement`, behind the existing `require_current_admin`, with a
  `/admin/users` page beside `/admin/invites`. Not the `ADMIN_TOKEN` `require_admin` — this is a
  human action, not a machine one. Grants and revocations are logged with the acting admin.
  Setting `entitled_until` to a past timestamp is how a grant is ended; rows are never deleted.
- **D-39 The gate has two kinds of checkpoint, and the batch half is the one that gets missed.**
  *Request-time:* `Depends(require_entitled())` on `/me/follows`, `/me/watchlist`,
  `/me/timeline`, `/me/import/*`, `/me/settings`, `/me/push` *(removed by ADR-0021)*. `/calendar/{token}.ics` answers
  404 rather than 403 — the token is unauthenticated and must not confirm that it is valid.
  *Batch-time:* the `notify` and `digest` passes filter unentitled users in exactly the place
  they already filter unverified ones (D-31), recording `status = suppressed`; derived-watchlist
  maintenance (D-13) skips unentitled users on the sweep credits pass. These passes fan out over
  all users instead of answering a request, so they get no protection from a route dependency.
- **D-40 Losing entitlement suppresses, never destroys.** Follows, watchlist items, dismissals,
  settings and the iCal token survive expiry untouched, so a later grant (or a subscription)
  restores the account exactly as it was. Nothing in this project deletes user graph rows.
- **D-41 The locked state is honest.** `AuthContext.user.entitled` boolean. For a signed-in,
  unentitled user `/` renders the global feed with a locked-timeline panel rather than an empty
  timeline; follow buttons and watchlist toggles render disabled with an explanatory tooltip
  rather than vanishing, so the product is legible to someone deciding whether to want it. Copy
  says access is currently limited while the subscription tier is built — not "upgrade now",
  because there is nothing to buy yet.

### Follow subsumes the watchlist (2026-09-20, NEU-1405 planning; ADR-0018)

- **D-42 One concept: follow.** Every follow feeds the timeline *and* alerts. The watchlist is
  the **computed** set of in-play films the user's follows cover for alerts, minus the films
  they have muted; it is no longer a record the user maintains. INV-7 ("follows produce
  timeline rows only") is withdrawn. The word "watchlist" stays as the name of the set; the
  film page shows one follow control and never says it.
- **D-43 Coverage on the follow** *(amended 2026-09-20 by D-47/D-48: three tiers `lead|major|any`,
  `all` renamed `major`, and `any` widens the timeline too)*. `app.follow.coverage ∈ lead|all`, default `lead`. For a
  person follow it picks which credits alert: `lead` = director or top-3 billing (D-13's cut,
  which is what keeps a prolific actor from becoming a push firehose), `all` = every
  seed-grade credit (D-11's cut). Company, franchise and title follows cover every matching
  film. Timeline coverage stays D-11 for every follow; coverage narrows alerts only.
- **D-44 One store setting per user** *(superseded 2026-09-26 by ADR-0021: the column and the setting are removed)*. `app.user_settings.alert_stores` (subset of
  `buy|rent|stream`, default `{stream}`) replaces per-item `alert_prefs`. No per-film
  preferences exist. The always-on push whitelist beats (D-32) are unchanged.
- **D-45 Mute, not dismissal.** Taking a film off the watchlist is a **mute**: it leaves
  alerts, the calendar, the iCal feed, the digest slate *and the timeline* (amended in the
  NEU-1414 planning session, 2026-09-20: a mute means "not interested in this film", and it
  silences the film everywhere, including events that only name a followed person on it), and
  can be undone. `app.watchlist_dismissal` holds mutes (table kept, vocabulary changed).
  `POST /me/watchlist {film_id}` means *want* (clear any mute; create a `manual` title follow
  if nothing else covers the film); `DELETE /me/watchlist/{film_id}` means *stop* (delete a
  direct title follow; mute if another follow still covers it). Both answer the resulting
  item. Importers write a title follow per film and no watchlist rows.
- **D-46 The alert window ends at `Canceled`, not at `Released`.** A person, company or
  franchise follow covers a film from announcement until `PROVIDER_POLL_MAX_AGE_DAYS` (365)
  after its primary release date, in every TMDB status but `Canceled`. `Released` is the state
  the home-release beats (D-26, D-28, D-35) land in, so a window that ended there delivered
  none of them to an indirect follower. `TMDB_EXCLUDED_STATUSES` keeps governing admission and
  the in-play working set; the window's own term is a constant
  (`catalog.queries.ALERT_WINDOW_DEAD_STATUSES`), because TMDB's vocabulary is closed and there
  is nothing to tune. Title follows are unchanged: any state. (NEU-1417, 2026-09-20.)

### Any credit, any person (2026-09-20, NEU-1416 planning)

- **D-47 The widest tier widens the timeline.** Timeline reach for a person follow is *seed
  grade OR the follow's own tier*. `lead` and `major` are subsets of seed grade, so D-11 holds
  for them unchanged; `any` reaches every credit the person holds on an in-play film. Coverage
  still never narrows the timeline. Amends D-11 and the last sentence of D-43.
- **D-48 Three tiers: `lead | major | any`.** `all` is renamed `major` (director,
  Writer/Screenplay, top-5: the seed grade) by a one-line data migration; `any` is every credit
  at any billing or crew job. A tier called "all" beside one that reaches more is the drift the
  glossary exists to stop. On screen: Lead roles, Major credits, Every credit.
- **D-49 A follow is its own admission rule for the credit history.** `catalog.film_credit_change`
  records seed-grade changes for everyone and *every* credit change for a person somebody
  follows at `any` (the **recorded grade**). Non-seed cast cards as `casting`, non-seed crew as
  `crew_attached`, through the same quarantine, burst grouping and sanity holds. Both sides of
  the diff use the same followed set at the same moment, so a new follow never fabricates an
  attachment; first observation stays a baseline. Not retroactive: existing credits are already
  in `film_credit` and reach the timeline and alerts the moment the follow widens.
- **D-50 Followed people are enumerated under a `followed` tranche.** The sweep's enumeration
  set is the seed people plus every live person followed at `any`; their non-seed undated
  credits are `followed` attachments, admitted only when `SWEEP_ADMIT_FOLLOWED` is on, under the
  same status filter and corroboration bar as the other tranches. Seed grade and the NEU-1090
  top-5 measurement are untouched (ADR-0013 amended). Spec `NEU-1416-follow-any-credit-depth.md`.

## 6. Milestones and shared contracts

Dependency-ordered. Each milestone's contracts are the cross-cutting agreements sibling tickets
(backend ↔ frontend, or two backend tickets) must share before either is built.

### M1 — Accounts and the delivery pipe

**Goal:** the app can send mail, verify addresses, take public signups safely, and meter abuse.
Absorbs *bl: Transactional Email* and *bl: Open Signup & Abuse Controls*. Nothing downstream
(onboarding, alerts, digests) can ship to real users without it.

**Shared contracts**
- `mail/` gateway interface: `send(to, template, context) -> MessageId`; templates are Jinja
  files under `mail/templates/`; provider adapter = Resend; `MAIL_PROVIDER`, `MAIL_FROM`,
  `RESEND_API_KEY` settings validated at startup.
- `app.user.email_verified_at` (nullable timestamp). Verification/reset tokens via
  `app/tokens.py` conventions; routes `POST /auth/verify/request`, `POST /auth/verify`,
  `POST /auth/reset/request`, `POST /auth/reset`, `POST /auth/email-change/{request,confirm}`.
- Signup: `POST /auth/signup` drops the invite requirement, takes `turnstile_token`; invites
  stay valid as an optional field. `TURNSTILE_SECRET` setting.
- Rate limiter: `Depends(rate_limit("signup"))`-style dependency, bucket names and limits in
  settings, 429 with `Retry-After`.
- Entitlement seam (D-37, D-38): `app.user.entitled_until` (nullable timestamp, default NULL);
  `app/entitlements.py` with `is_entitled(user)` and the `require_entitled()` dependency;
  `/admin/users` list + grant/revoke routes behind `require_current_admin`. The seam must land in
  M1 because M3 and M7 tickets cite it; it gates nothing until those tickets apply it.
- Frontend: `/verify`, `/reset`, `/forgot`, `/email-change` routes; the last one lands the
  confirmation link `POST /auth/email-change/request` mails to the new address and posts its
  token to `POST /auth/email-change/confirm` (NEU-1341);
  `AuthContext.user.email_verified` and
  `AuthContext.user.entitled` booleans; `/admin/users` grant page.

### M2 — Claim ledger and the event/state split

**Goal:** `news.event` becomes the claim ledger (ADR-0017): supersession is recorded and
visible, confidence is visible on every card, and the film page discloses first-seen dates. The
foundation every later milestone reads.

**Shared contracts**
- `news.event.status` (`published` | `superseded`, default `published`),
  `news.event.superseded_by` (nullable FK). Migration + `ck_event_status`.
- `EventOut` gains `status`, `superseded_by`, `occurred_at`, keeps `confidence`. `FeedItem`
  unchanged otherwise.
- Frontend `EventCard` renders a confidence badge (`confirmed` / `unconfirmed`) and, when
  `status=superseded`, a "later retracted" marker linking to `superseded_by`.
- Supersession write (D-2) lives in `ingest/sweep/credit_events.py`: carding a `credit_removed`
  event marks the prior attachment card(s) for each named person.

### M3 — Follow graph, timeline, and import

**Goal:** turn the firehose into a feed about what the user cares about, and solve cold start:
signup → one CSV upload → populated timeline.

**Shared contracts**
- Tables: `app.follow (user_id, entity_type, entity_id, source, created_at)` unique per
  `(user, type, id)`; `app.watchlist_item (user_id, film_id, source, alert_prefs TEXT[],
  created_at)`; `app.watchlist_dismissal (user_id, film_id)`.
- API: `GET/POST/DELETE /me/follows`, `GET/POST/PATCH/DELETE /me/watchlist`,
  `GET /me/timeline` (same response shape as `/feed/grouped`, filtered per D-11),
  `GET /people/search?q=`, `GET /people/popular`, `GET /companies/search?q=`,
  `GET /collections/search?q=`, `POST /me/import/letterboxd` (multipart),
  `GET /me/import/tmdb/start` → redirect, `GET /me/import/tmdb/callback`.
- TMDB client gains `/search/movie` (Letterboxd/TMDB title resolution) here; `/search/person`
  arrives in M4.
- Entity follow buttons on film page (title, director, top cast, companies, collection).
- Onboarding route `/welcome` with the three D-17 steps; `/` swaps to timeline when signed in.
- Every `/me/*` route above carries `Depends(require_entitled())` (D-39); derived-watchlist
  maintenance skips unentitled users. `/people/*`, `/companies/*`, `/collections/*` search stays
  public — it feeds the film page. Unentitled signed-in users get the D-41 locked state, not an
  empty timeline.

### M4 — Person resolution

**Goal:** link a trade story's named person to a TMDB person id, deterministically where
possible, with every decision inspectable and the wrong-Chris-Evans failure impossible to alert on.

**Shared contracts**
- Cluster output schema extension (D-20) and its `prompt_version` bump.
- Tables `news.story_person`, `news.resolution_cache` (D-24).
- Gateway stage `resolve` (D-22) with `RESOLVE_PROVIDER`/`RESOLVE_MODEL` settings.
- Timeline query (M3) gains the "events naming a resolved followed person" branch.
- `GET /admin/resolution` (D-25) and its frontend page.

### M5 — Quarantine and the publication queue

**Goal:** publish every reliable item, suppress slop, never bury a released item.

**Shared contracts**
- `SWEEP_CREDIT_QUARANTINE_HOURS` (default 72, 0 disables) in `config.py` and the deploy
  checklist (Coolify shadows compose fallbacks).
- Burst-collapse grouping key (D-7): `(film_id, event_type, sweep pass)`.
- Sanity-hold log: `ingest.credit_hold (film_id, person_id, changed_at, reason, held_at,
  released_at)`.
- Tier-A short-circuit hook in `link/cluster.py` (D-5); it reads the quarantine state the M2
  supersession write and the D-3 hold share.
- Spike output: a markdown report under `docs/specs/` with the survival curve and the
  recommended window.

### M6 — Home release

**Goal:** tell users when they can watch a film at home — forward-looking dates and observed
availability — without ever tracking churn.

**Shared contracts**
- `release_grade.py` widening (D-26) — buckets `digital`, `physical`; `public.release` labels.
- Tables `catalog.availability_first_seen (film_id, region, provider_id, monetization_type,
  first_seen_at)`, `catalog.watch_provider (id, name, logo_path)`; `ingest_run.kind =
  "providers"`.
- Event type `now_available` registered in `ck_event_type`, `_EVENT_STAGE` (ranks above
  `release_date`), excluded from LLM vocabularies.
- `FilmDetail.where_to_watch: {region: "US", flatrate: [...], rent: [...], buy: [...],
  link: str | None, attribution: "JustWatch"} | None` — `None` when no poll has found the
  film anywhere. `link` is TMDB's per-film watch page; it and `attribution` are what TMDB's
  terms require (link back, credit JustWatch), not decoration. Provider entries are
  `{id, name, logo_path}` (NEU-1376).
- Calendar DTO gains `bucket ∈ premiere|limited|wide|digital|physical`.

### M7 — Notifications, digest, and calendar

**Goal:** deliver without bombarding. Follows → timeline; watchlist → alerts; everything else →
digest; the product lives in the user's calendar.

**Shared contracts**
- `app.notification` (D-31), `app.user_settings (digest_cadence, ical_token)`,
  `app.push_subscription` *(removed by ADR-0021)*.
- Decision pass entrypoint `python -m upmovies.pipeline_run notify` (Coolify slot after the
  daily chain) + `digest {daily|weekly}`.
- Email templates: `alert` *(removed by ADR-0021)*, `digest`, `slate`.
- `GET /calendar/{token}.ics` (404 for an unentitled owner, D-39); `GET/PATCH /me/settings`;
  `POST/DELETE /me/push` *(removed by ADR-0021)* — both `require_entitled()`.
- The notify and digest passes suppress unentitled users beside unverified ones (D-39): one
  predicate, `status = suppressed`, asserted by a test per pass.
- `/movie/{id}/videos` polling shares the D-27 scoped set; `trailer` event body carries the
  YouTube key.

### M8 — Follow subsumes the watchlist

**Goal:** one user-facing concept instead of two (D-42 to D-45, ADR-0018). Story NEU-1413;
backend NEU-1414 first, then the film page (NEU-1405) and the pages (NEU-1415). The
entitlement gate (D-37 to D-41) is untouched.

**Shared contracts**
- `app.follow.coverage ∈ lead|all` default `lead` (person follows only; ignored for the other
  types). `app.user_settings.alert_stores TEXT[] default {stream}`. `app.watchlist_item.alert_prefs`
  dropped. `app.watchlist_dismissal` kept as the mute table.
- Watchlist = computed, **queried, never materialised** (settled in NEU-1414's planning,
  2026-09-20; `app.watchlist_item` is dropped). A person, company or franchise follow covers a
  film inside the **alert window**: not `Canceled`, and primary release date NULL or no
  older than `PROVIDER_POLL_MAX_AGE_DAYS` (365 — the provider poll's own ceiling, so alerts and
  the `now_available` cards that feed them stop together); a title follow covers its film in any
  state. Minus mutes. One query builder in `app/follow_queries.py` is what `GET /me/watchlist`,
  `/me/calendar`, the iCal feed, the notify and digest passes, the slate mail and the provider
  and video polls' "somebody is waiting on this film" rule (D-27 rule 2) all read.
- `GET /me/watchlist` → `{items: [{film, covered_by: [{entity_type, entity_id, name}],
  followed, muted, created_at}]}`, muted films included, direct title follow first in
  `covered_by`. `POST /me/watchlist {film_id}` = want; `DELETE /me/watchlist/{film_id}` = stop;
  `POST` returns the item (`200`); `DELETE` returns the item (`200`) while another follow
  still covers the film, now muted, and `204` once nothing covers it (there is no item left to
  answer). `PATCH /me/watchlist/{film_id}` removed. `POST /me/follows` takes
  optional `coverage`; `PATCH /me/follows/{entity_type}/{entity_id} {coverage}` new;
  `FollowOut.coverage`. `GET/PATCH /me/settings` carry `alert_stores`.
- Migration, once: every non-derived `watchlist_item` row (`manual`, `letterboxd_import`,
  `tmdb_import`) with no title follow yet → a title follow with the row's own `source` and
  `created_at`; `derived_from_follow` rows dropped (their follows recompute them); dismissals
  unchanged; the table dropped with `alert_prefs`.
- Frontend: one `TitleFollowButton` under the film title with a static cue line, three access
  states as today (NEU-1405); `/me/watchlist` without chips or a removal confirm, with "via …"
  and a Muted section; `/me/follows` with the per-person coverage control; `/settings` with the
  store set (NEU-1415).

### M9 — Any credit, any person

**Goal:** a subscriber can follow *any* person from that person's own page and hear about any
credit they pick up (D-47 to D-50). Story NEU-1416; backend ticket first, then the frontend
ticket. Seed grade, the alert window, mutes and entitlement are untouched.

**Shared contracts**
- `app.follow.coverage ∈ lead|major|any`, default `lead`; `all` → `major` by migration.
  `FollowOut.coverage`, `POST`/`PATCH /me/follows` carry the three values; the
  `422 coverage_not_applicable` rule for non-person follows stands.
- Timeline: seed grade OR the follow's tier (`any` = every credit); alerts: the tier's cut inside
  the alert window. One `credit_tier(credit_type, job, credit_order)` in `app/follow_queries.py`
  is what the person page's badges and the alert query both read.
- Credit history records the **recorded grade**: seed grade, plus every credit of a person
  followed at `any` (`people_followed_at_any()` query builder, no entitlement filter, as the poll
  set). Roles gain `crew` (→ `crew_attached`); no new event type.
- Sweep: enumeration set = seeds ∪ people followed at `any`; role `followed`; flag
  `SWEEP_ADMIT_FOLLOWED` (default off) beside the three tranches.
- `GET /people/{ref}` (`<id>-<slug>`, public, 404 for unknown or TMDB-deleted) →
  `{ref, id, name, profile_path, known_for_department, birthday, deathday, upcoming: [...],
  recent: [...]}`; each film row carries `film` (watchlist-row shape with `headline_release`),
  `credits: [{credit_type, job, character, credit_order, tier}]` and the film's narrowest `tier`.
  `upcoming` = in play; `recent` = inside the alert window but not in play; nothing older.
- `GET /films/{ref}` sends the full cast (no 12 cap).
- Frontend: `/person/:ref` in the public layout with canonical-ref redirect; follow button plus a
  three-tier `CoverageControl` (Lead roles / Major credits / Every credit) that posts the tier on
  first follow and patches after; Upcoming and Recently released sections with tier badges. Film
  page cast and crew names link to the person page, buttons stay on seed-grade rows.
  `/me/follows` uses the same control and links person rows internally. Header entity search and
  company/collection pages are a separate story.

Spec: `docs/specs/NEU-1416-follow-any-credit-depth.md`.

## 7. Prerequisites and deploy notes

- `RESEND_API_KEY`, `TURNSTILE_SECRET`, `VAPID_*` *(removed by ADR-0021)*, `TMDB_*` user-auth redirect URL, and
  `RESOLVE_PROVIDER`/`RESOLVE_MODEL` are new Coolify variables — set in the UI after first deploy
  (compose fallbacks are seeds, not defaults).
- The sweep gains phases (`quarantine`, `providers`, `videos`); watch the sweep runtime and
  `record_progress` heartbeat contract.
- `SWEEP_ADMIT_FOLLOWED` (M9) is a fourth tranche flag, default off; flip it in Coolify after the
  NEU-1416 backend deploys, or followed people are enumerated but never admit.
- Everything runs in the container via `task`; before claiming any ticket done: `task format`,
  then `task test && task lint && task typecheck` (backend) / `task test && task lint && task
  typecheck` (frontend).

## 8. Open items (not blocking)

- Quarantine window value — resolved by the M5 spike; 72h until then.
- Whether resolution needs a correction UI — revisit once `/admin/resolution` shows volume.
- Franchise coverage via collections — measure after M3; hand-curated overlay if poor.
- Consumer pricing — separate project. This project ships the enforcement points and the admin
  grant (D-37 to D-41); *bl: Subscription & Billing* (NEU-262) replaces the grant with paid
  writes to `entitled_until` and revisits the signup copy.
