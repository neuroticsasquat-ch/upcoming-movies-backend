# NEU-1416 — Follow any person at any credit depth, from a person page

**Target repos:** upcoming-movies-backend (first), upcoming-movies-frontend (blocked by it)

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1416 (story)
**Milestone:** M9 — Any credit, any person (shared contracts live on the milestone)
**Children:** backend ticket, then frontend ticket — see the story's sub-issues
**Decisions:** D-47 to D-50 in `bl-consumer-pivot-project-spec.md` (D-11 and D-43 amended);
ADR-0013 amended (followed people join the enumeration set); ADR-0018 unchanged
**Ground truth read:** `docs/specs/NEU-1414-computed-watchlist-over-follows.md`,
`docs/specs/NEU-1417-alert-window-status-term.md`, `app/follow_queries.py`,
`catalog/seed_grade.py`, `ingest/sweep/seeds.py`, `ingest/tmdb/credit_history.py`,
`ingest/sweep/credit_events.py`, `public/service.py`, frontend `FilmCredits.tsx`,
`FilmCrew.tsx`, `FollowEntitySearch.tsx`, `MyFollows.tsx`, `lib/film-entities.ts`

## What to build and why

A subscriber can follow *any* person, from that person's own page, and hear about *any* credit
they pick up on an upcoming film or a recent home-media release — not only the roles the
seed-grade cut admits.

Today a person can be followed from a film page or by search on `/me/follows`, and what the
follow reaches is cut three times in three places:

| Surface | Cut | Where |
| -- | -- | -- |
| Timeline (D-11) | seed grade: director, Writer/Screenplay, top-5 billed | `follow_queries.followed_film_ids` |
| Alerts (D-43) | `lead` (director / top-3) or `all` (seed grade) | `follow_queries.covered_film_ids` |
| Ingest | seed grade, twice: the sweep enumerates seed people only, and the credit history diffs seed-grade credits only | `sweep/seeds.py`, `tmdb/credit_history.py` |

The ingest cut is the real ceiling: a minor credit added to an upcoming film never becomes a
`film_credit_change` row, so it never cards and never alerts, whatever a follow says. But the
catalog already holds **every** cast and crew credit of every film it has (`_upsert_credits`
rebuilds `catalog.film_credit` from the whole payload, and the refresh phase re-fetches every
in-play film each run). So a wider follow can read existing minor credits the moment it is
created; only *future* credit changes need ingest work, and only for the people somebody
actually follows that widely.

There is no person page and no `GET /people/{id}`; cast and crew names on the film page are
not links; the film page sends only the top 12 cast.

### Decided in the planning session (2026-09-20)

1. **The widest tier widens the timeline too.** D-11 said the timeline is always seed grade
   whatever the coverage. A user following a 12th-billed actor at the widest tier would be
   alerted about a film that never appears on their timeline. Timeline reach becomes *seed
   grade OR the follow's own tier*: `lead` and `major` are subsets of seed grade, so nothing
   changes for them; `any` reaches every credit on both surfaces. (D-47)
2. **Tiers are `lead | major | any`.** `all` (every seed-grade credit) is renamed `major`,
   because a tier called "all" beside one that reaches more is exactly the vocabulary drift
   the glossary exists to stop. Rows at `all` shipped the same day, so the rename is a one-line
   data migration. (D-48)
3. **A follow is its own admission rule for the credit history.** The diff covers every credit,
   but a non-seed change is recorded only for a person somebody follows at `any`. The
   resulting cards go through the same quarantine, burst grouping and sanity holds as
   seed-grade ones. Cards are global; a minor credit card on the global feed is real news
   about the film, and few of them will exist. (D-49)
4. **People followed at `any` are enumerated by the sweep under a `followed` tranche.** A
   candidate reached only through their non-seed credit is admitted under a new flag,
   `SWEEP_ADMIT_FOLLOWED`, subject to the same status filter and corroboration bar as the
   seed-grade tranches. Seed grade itself, and the NEU-1090 top-5 measurement, are untouched.
   (D-50)
5. **The person page lists upcoming and recently released films only** — in-play films, then
   films inside the alert window but past — every credit, each tagged with the narrowest tier
   that reaches it. Nothing older: that is exactly the set a follow can reach.
6. **Following from the person page defaults to `lead`**, like everywhere else; the three-tier
   control sits beside the button and each film row shows which tier reaches it, so the user
   sees that "Lead roles" reaches two of seven and widens if they want.
7. **The film page links every name, keeps its follow buttons where they are** (seed-grade
   rows, where `lead` is a sensible default), and the backend stops capping cast at 12.
8. **Header entity search and company/collection pages are a new story.** This one makes
   person rows on `/me/follows` and in the add-follow results link to the person page instead
   of TMDB, so a person found by search has a page to land on.

## Design

### D-1416.1 — Three tiers: `lead | major | any`

- `app/models.py`: `FOLLOW_COVERAGES = ("lead", "major", "any")`; `ck_follow_coverage`
  re-issued. `DEFAULT_COVERAGE` stays `lead`.
- Migration, once: `UPDATE app.follow SET coverage = 'major' WHERE coverage = 'all'`, then
  drop and recreate `ck_follow_coverage` (autogenerate does emit table CHECK changes for
  existing tables only as a manual op — write it by hand; run the schema-parity test).
  Downgrade maps `major` back to `all` and refuses (raises) if any `any` row exists.
- `app/dto.py`: `FollowCoverage = Literal["lead", "major", "any"]`. `FollowOut.coverage`,
  `POST /me/follows`, `PATCH /me/follows/{entity_type}/{entity_id}` accept the new values;
  `422 coverage_not_applicable` for a non-person follow is unchanged.
- **What `any` means**: every `catalog.film_credit` row the person holds on the film — any
  billing position including unbilled (`credit_order IS NULL`), any crew job. The SQL spelling
  is "the credit exists", so `_coverage_credit_clause` gains a third arm:
  `coverage == "any"` → `true()` (the join to `film_credit` already restricts to the person's
  credits).
- **The tier a credit belongs to, as one function.** `app/follow_queries.py` gains
  `credit_tier(credit_type, job, credit_order) -> Literal["lead","major","any"]`: `lead` when
  director or `credit_order < LEAD_TOP_BILLED_ORDER`; `major` when `is_seed_grade`; `any`
  otherwise. It lives beside `lead_credit_clause` because the lead cut is defined there, and
  it is what `GET /people/{ref}` reads for the per-credit badge, so the badge and the alert
  query cannot disagree.

### D-1416.2 — Timeline reach = seed grade OR the follow's tier (D-11 amended)

- `followed_film_ids`' person branch stops reading `followed_tmdb_ids(user, "person")` and
  joins `_int_follows("person", user_id=...)` instead, so it can read `coverage` per row:
  `seed_grade_credit_clause() OR follows.c.coverage == "any"`. Still bounded by
  `in_play_clause`, still minus mutes. The docstring's "Coverage is not read here" paragraph
  is rewritten: coverage never *narrows* the timeline, and only `any` widens it.
- `events_naming_followed_people` is unchanged: a mention is a mention whatever the tier.
- Test: a user following a person at `any` sees events on an in-play film where that person
  is 12th-billed; the same follow at `lead` or `major` does not; the same follow at `any` does
  not see that film once it is muted or out of play.

### D-1416.3 — Alerts at `any`

`covered_film_ids`, `covering_follows` and `covered_by_any_user_clause` all read
`_coverage_credit_clause`, so the third arm reaches them without further change. The alert
window and the mute rules are untouched. Test: at `any`, a 12th-billed credit on a film
inside the alert window puts the film on the watchlist, in `covering_follows` with
`entity_type = person`, and in the poll set; at `major` it does not.

### D-1416.4 — The credit history records what a follow asks for (D-49)

`catalog.film_credit_change` today holds seed-grade changes only, by way of `is_seed_grade` on
both sides of the diff. The rule becomes **recorded grade**: a credit is recorded when it is
seed grade **or** its person is in the *followed-at-any set*.

- `app/follow_queries.py` gains `people_followed_at_any() -> Select[tuple[int]]`:
  `SELECT DISTINCT cast(entity_id AS int) FROM app.follow WHERE entity_type = 'person' AND
  coverage = 'any' AND entity_id ~ '^[0-9]+$'` — the module's SELECT-list cast discipline. No
  entitlement filter, on the same reasoning as `covered_by_any_user_clause`: D-40 keeps a
  lapsed user's follows, and the poll set does not filter either.
- `ingest/tmdb/credit_history.py`: `seed_credits_from_details(details, *, followed)` and
  `load_seed_credits(session, film_id, *, followed)` take the set of followed person ids and
  keep a credit when `is_seed_grade(...) or person_id in followed`. Rename to
  `recorded_credits_from_details` / `load_recorded_credits` / `diff_recorded_credits`
  (`RecordedCredit`), and say in the module docstring what "recorded grade" is. The set is
  loaded once per `upsert_film` call, before the rebuild (`_upsert_credits` already reads the
  stored side there), through the query builder above — ingest reading `app` has precedent in
  `ingest/providers.py`.
- **Both sides of the diff are computed with the same followed set at the same moment**, so a
  follow created between two observations does not turn a credit that was present all along
  into a phantom `added` row: it is in `previous` and in `current` alike. And **first
  observation stays a baseline**: `previous is None` still returns nothing.
- **Role vocabulary.** `catalog/seed_grade.py` gains `recorded_role(credit_type, job) ->
  str`: `credit_role(...)` when that is not None, else `"cast"` for a cast credit and
  `"crew"` for a crew credit. `news/catalog_events.py`: `CREDIT_ROLE_EVENT_TYPES["crew"] =
  "crew_attached"` (`cast` already maps to `casting`). `ROLE_ORDER` gains `"crew"` after
  `"cast"` (and `"followed"` last — D-1416.5). No new event type; `ck_event_type` is
  unchanged.
- `ingest/sweep/credit_events.py`: everywhere `credit_role` is read to derive an
  `AttachedCredit.role` or to match a detachment, read `recorded_role`. The quarantine gate's
  `present_seed_credits` becomes `catalog.queries.present_recorded_credits(session, *,
  film_ids, followed)` and answers "is this credit still there under the same recorded role"
  — for a followed person's non-seed credit that is "still a cast credit at any billing" /
  "still a crew credit with this job". The gate checks presence, **not** whether the person is
  still followed at `any`: a card already recorded publishes even if the follow was narrowed
  meanwhile. Rare and harmless, and it keeps the gate a property of the film.
- Burst grouping is by `(film, event_type, pass)`, so a cinematographer's `crew_attached`
  joins a director's in the same pass; the body's people list names both, with roles. The
  D-7 billing tiebreak reads `credit_order`, which a non-seed cast credit carries as usual.
- Sanity holds (D-8) apply unchanged: the per-person-per-day burst and the birth/death checks
  judge the person, not the grade.
- Detachments (`credit_removed`, NEU-1200) follow the same rule: a followed person's non-seed
  credit disappearing is recorded and carded, and supersedes their attachment card as today.
- Not retroactive: following someone at `any` records their *future* changes. Their existing
  credits are already in `film_credit` and reach the timeline and alerts immediately through
  D-1416.2/3 — which is why no backfill is needed.
- Tests: a 12th-billed credit added to an observed film is recorded when someone follows the
  person at `any` and not when they follow at `major`; a follow created between two
  observations of an unchanged credit records nothing; the recorded row clears quarantine and
  cards as `casting` naming the person; a non-seed crew job records and cards as
  `crew_attached`; first observation is still a baseline.

### D-1416.5 — The sweep enumerates followed people under a `followed` tranche (D-50)

- `ingest/sweep/seeds.py::load_seed_person_ids` returns the union of today's seed query and
  `people_followed_at_any()`, still excluding `person.tmdb_missing_at IS NOT NULL` (a
  tombstoned person is not a follow target, `_LIVE_PERSON`). The docstring says why: a follow
  is its own admission rule, separate from seed grade.
- `seed_attachments(person_id, credits, *, followed: bool)`: for a followed person, an undated
  credit that is **not** seed grade is an attachment with role `"followed"`; their seed-grade
  credits keep their normal role. A seed person who is also followed is enumerated once (set
  union) and gets both kinds of attachment.
- `AdmissionTranches` gains `followed: bool` from `SWEEP_ADMIT_FOLLOWED` (default `False`,
  like the other three); `admits(roles)` admits when `self.followed and "followed" in roles`.
  Master `SWEEP_ENABLED` still gates everything.
- Corroboration is unchanged: a followed person counts as one distinct reaching person, and
  the threshold (default 1) applies. Status filtering (`classify_skip`) is unchanged.
- `config.py` documents the flag beside the other tranches, and `validate_sweep_configuration`
  treats it like them. `summary.sweep_detail` reports `followed` in the role breakdown.
- Tests: a followed person's undated non-seed credit is a `followed` attachment; the film is
  admitted only when `SWEEP_ADMIT_FOLLOWED` is on; a person followed at `major` is not
  enumerated unless they are a seed anyway; a tombstoned followed person is skipped.

### D-1416.6 — `GET /people/{ref}`

- `catalog/ref.py`: `person_ref(person_id, name)` and `parse_person_ref(ref)` beside the
  film pair, same slug rule, same fallback to the bare id.
- `routers/public.py`: `GET /people/{ref}` → `PersonDetailResponse`, public, `_public_limit`,
  resolved on the leading id; `404 person not found` for an unknown id or one with
  `tmdb_missing_at` set. The response's own `ref` is canonical; the client redirects when it
  differs, as the film page does. Registered **before** nothing — `/people/search` and
  `/people/popular` are literal paths and FastAPI matches them first, but add a test that
  `GET /people/search?q=x` still reaches search.
- `public/dto.py`:

  ```
  PersonDetailResponse:
    ref: str                       # "<id>-<slug>"
    id: int                        # TMDB person id, the follow entity_id stringified
    name: str
    profile_path: str | None
    known_for_department: str | None
    birthday: date | None
    deathday: date | None
    upcoming: list[PersonFilmOut]  # in play, headline release asc nulls last, then title
    recent: list[PersonFilmOut]    # alert window but not in play, headline release desc

  PersonFilmOut:
    film: WatchlistFilmOut-shaped: ref, id, tmdb_id, slug, title, poster_path,
          headline_release (NEU-1397's helper, so the row cites the date the film page shows)
    credits: list[PersonCreditOut]  # one row per film; a writer-director has two entries
    tier: "lead" | "major" | "any"  # the narrowest tier across the film's credits

  PersonCreditOut:
    credit_type: "cast" | "crew"
    job: str | None
    character: str | None
    credit_order: int | None
    tier: "lead" | "major" | "any"   # credit_tier(...)
  ```

  `upcoming` = `in_play_clause(today, TMDB_EXCLUDED_STATUSES)`; `recent` =
  `alert_window_clause(today, PROVIDER_POLL_MAX_AGE_DAYS) AND NOT in_play`. Both read
  `catalog.film_credit` for the person (index `ix_catalog_film_credit_person`). Nothing older
  is returned.
- `public/service.py::get_person_detail`. Reuse the headline-release helper and
  `WatchlistFilmOut`'s shape rather than a new film summary.
- Tests: refs resolve on the id and answer the canonical ref; 404 for unknown and tombstoned;
  a film appears in `upcoming` or `recent` and never both; a film past the window is absent;
  a writer-director yields one row with two credits and tier `lead`; a 12th-billed credit has
  tier `any`; a 4th-billed credit has tier `major`.

### D-1416.7 — The film page sends the full cast

`get_film_detail` drops `.limit(12)` on the cast query. Crew was never capped. The frontend's
`FOLLOWABLE_CAST_COUNT` stays 5, so the buttons do not move.

### D-1416.8 — Frontend (blocked by the backend ticket)

- **Route** `person/:ref` → `routes/person.tsx` in the public layout beside `film/:ref`
  (server-rendered, anonymous-readable). Redirect to the canonical ref when the response's
  differs, exactly as `routes/film.tsx` does. `api/public.ts` gains `getPerson(baseUrl, ref)`;
  `api/types.ts` the DTOs above; `FollowCoverage` becomes `"lead" | "major" | "any"`.
- **`lib/film-entities.ts`**: `personPath(id, name)` → `/person/<id>-<slug>` (slug rule as the
  backend's; the page redirects to canonical anyway, so a client-side slug only has to be
  stable). `personTarget` unchanged.
- **Person page**: header (photo via `profileUrl`, name, department, born/died where TMDB has
  them); the follow control; two sections, **Upcoming** and **Recently released**, each a list
  of rows reusing the watchlist row look (poster, title linking to the film page, headline
  release) plus a credit line ("Director · Writer", or "Character") and a **tier badge**
  reading Lead, Major or Any from `film.tier`. Empty states: "No upcoming films in the
  catalog" / "No recent releases". The page calls `rememberFollowLabel("person", id, name,
  profile_path)` on load so `/me/follows` shows the name.
- **The follow control**: `components/follow/CoverageControl.tsx`, extracted from
  `MyFollows.tsx`, with three radios — **Lead roles** (`lead`), **Major credits** (`major`),
  **Every credit** (`any`). On the person page it is always visible beside the
  `FollowButton`: while not following, its value is local state (default `lead`) that the
  follow sends as `coverage` on `POST /me/follows` (`createFollow` gains an optional
  coverage); while following, it reads the follow's coverage and a change goes through
  `useUpdateFollowCoverage`. The three access states (anonymous / locked / entitled) are the
  `FollowButton`'s as today; the control is disabled in the first two.
- **The note over the People group on `/me/follows`** is rewritten, because it is now wrong:
  "Lead roles and Major credits narrow what we alert you about; your timeline shows every
  major credit either way. Every credit widens both." The labels change from "Lead roles only"
  / "Every credit" to the three above.
- **Film page**: in `FilmCredits.tsx` and `FilmCrew.tsx` every name becomes a `Link` to
  `personPath(...)` when `person_id` is present (a payload without ids renders plain text, per
  the existing null-target rule). Buttons stay on the same rows. The cast list now holds the
  full cast; the section is collapsed by default, and the count in its title reflects it.
- **`/me/follows`**: `tmdbUrl` for a person row becomes the internal person path (companies
  and collections keep their TMDB link until the new story gives them pages).
  `FollowEntitySearch` person results link their name to the person page.
- Tests (vitest + msw): the page renders both sections and the badges from a fixture; the
  control posts `coverage` on first follow and patches after; canonical-ref redirect; film
  page names are links; the follows note copy.

### D-1416.9 — Configuration and deploy

- `SWEEP_ADMIT_FOLLOWED` in `config.py`, compose fallback `false`, documented on the deploy
  checklist. **Flipping it in Coolify is owed after deploy** and is what makes D-1416.5 live;
  D-1416.4 (the credit history) needs no flag — it records for whoever is followed.
- The `all` → `major` migration runs in the backend deploy; the frontend must not ship its
  `FollowCoverage` change first (it would send `major` to a backend that rejects it), which is
  why the frontend ticket is blocked by the backend one.

### D-1416.10 — Documents

- Project spec: D-47 to D-50 under a new heading, D-11 and D-43 annotated as amended, §6 M9
  with the shared contracts, §7 deploy note for the flag.
- `ADR-0013` gains an amendment paragraph: followed people join the enumeration set under
  their own tranche; the seed-grade definition is not widened.
- `CONTEXT.md` (backend): **Coverage** rewritten for three tiers and the timeline rule;
  **Follow** says the timeline cut is seed grade or the follow's tier; **Seed person** notes
  the followed-at-any union; **Tranche** lists the fourth flag; new entry **Recorded grade**.
  Frontend `CONTEXT.md` needs no new term — it defers to the backend for coverage.

### What does not change

- Seed grade (`TOP_BILLED_ORDER = 5`, `WRITER_JOBS`, producers excluded) and the NEU-1090
  measurement. `LEAD_TOP_BILLED_ORDER = 3`.
- The alert window (D-46), mutes (D-45), the store setting (D-44), the want/stop routes,
  importers, the migration NEU-1414 ran, entitlement gating (D-37 to D-41).
- `events_naming_followed_people`, resolution (`story_person`), the LLM vocabularies.
- The film page's follow buttons and their rows; `/people/search`, `/people/popular` and the
  onboarding grid.

## Acceptance criteria

### Backend — `app/`

- `FOLLOW_COVERAGES == ("lead", "major", "any")`; migration renames `all` → `major` and
  re-issues `ck_follow_coverage`; `test_migration_schema_parity` green; no row reads `all`.
- `credit_tier` and `people_followed_at_any` exist in `app/follow_queries.py` with tests.
- `followed_film_ids`: `any` reaches every credit on in-play films; `lead`/`major` unchanged;
  mutes still subtract.
- `covered_film_ids` / `covering_follows` / `covered_by_any_user_clause`: `any` reaches every
  credit inside the alert window.
- `POST`/`PATCH /me/follows` accept `major` and `any`; `FollowOut.coverage` returns them.

### Backend — `ingest/`, `catalog/`

- `credit_history` records seed-grade changes for everyone and every change for people
  followed at `any`; baseline rule intact; no phantom `added` when a follow appears between
  observations.
- `credit_events` cards a followed person's non-seed cast credit as `casting` and non-seed
  crew credit as `crew_attached`, after quarantine, with holds and supersession as today.
- `load_seed_person_ids` includes live people followed at `any`; `seed_attachments` yields
  `followed` attachments; `SWEEP_ADMIT_FOLLOWED` gates their admission; `sweep_detail` reports
  the role.

### Backend — `public/`

- `GET /people/{ref}` per D-1416.6, with the tests listed there; `/people/search` and
  `/people/popular` still route.
- `GET /films/{ref}` returns the full cast.

### Frontend

- `/person/:ref` renders, redirects to canonical, shows Upcoming and Recently released with
  tier badges, follow button and three-tier control; first follow posts the chosen tier.
- Film page cast and crew names link to the person page; buttons unchanged; full cast listed.
- `/me/follows`: three-tier control with the new labels and note; person rows link internally.
- `FollowEntitySearch` person results link to the person page.

### Tooling

Backend: `task format`, then `task test && task lint && task typecheck`. Frontend: the same.
Do not run the backend suite concurrently with another pytest against `app_test`.

## Out of scope / deferred

- **Header entity search, and company and collection pages** — the new story filed from this
  session (see the M9 milestone). This ticket links person rows to the person page only.
- People in the sitemap; a biography field (TMDB's `/person/{id}` carries one; the model does
  not).
- Retroactive carding when a follow widens to `any`; the existing credits already reach the
  timeline and alerts.
- A tier below `lead`, or per-job coverage ("only when they direct").
- Tuning `SWEEP_CORROBORATION_THRESHOLD` for the followed tranche; it is 1 today.
