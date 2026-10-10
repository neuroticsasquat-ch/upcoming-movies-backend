# NEU-1542 — The calendar splits by where you watch, physical leaves the model, and the where-to-watch box goes

**Ticket:** [NEU-1542](https://linear.app/neuroticsasquatch/issue/NEU-1542) (no priority, no comments, no relations)
**Project:** bl: Maintenance (no milestone, no project spec, no shared contracts)
**Target repos:** upcoming-movies-backend (this file) **and** upcoming-movies-frontend
(`../../../frontend/docs/specs/NEU-1542-calendar-split-and-initial-release-only.md` holds the
frontend half in full; the decisions below are shared). **Base branch:** `main` in both.
**Deploy order:** free in either direction (§5). One migration (drops a table). One one-off
prod script, run by Tom after deploy (§3.6). No Coolify change.
**Related:** D-26 (home-release buckets; amended), D-27/D-28 (provider poll and `now_available`;
untouched), D-29 (where-to-watch box; superseded), D-34 (iCal buckets; amended), DC-9 (slate
buckets; amended), FB-26 (slate = my-films calendar; holds), NEU-1411 (`/me/calendar`),
NEU-1412 (the tabbed calendar page), NEU-1532 (released films card home-release beats only;
its release-date pass now records digital alone), ADR-0014, **ADR-0023** (written with this
spec: `docs/adr/0023-the-site-tracks-a-film-to-its-first-home-availability.md`).
**Ground truth read (2026-10-08):** `catalog/release_grade.py`, `public/release.py`,
`public/service.py` (`CALENDAR_REGION`, `_CALENDAR_BUCKET_ORDER`, `_calendar_governing_cte`,
`_calendar_page`, `get_calendar`, `get_my_films_calendar`, `_my_films_visible`,
`get_ical_feed`, `_where_to_watch`, `get_film_detail`), `public/ical.py`, `public/dto.py`,
`routers/public.py`, `routers/me_calendar.py`, `ingest/providers.py`, `ingest/release_dates.py`,
`ingest/sweep/release_events.py`, `ingest/tmdb/release_date_history.py`,
`ingest/tmdb/upsert.py::rebuild_release_dates`, `catalog/models.py` (`FilmReleaseDate`,
`FilmReleaseDateChange`, `WatchProvider`, `AvailabilityFirstSeen`, `FilmAvailabilityCurrent`),
`app/services/digest_sender.py` (`load_slate`, `load_slate_markers`, `_bucket_rank`),
`synthesize/deterministic.py`, `scripts/prune_released_trailer_cards.py`,
`scripts/rerender_now_available_summaries.py`, `CONTEXT.md` **Home-release date**, **Slate**,
**Now-available event**, **My films calendar**; frontend `components/calendar/*`,
`routes/calendar.tsx`, `routes/film.tsx`, `components/film/{WhereToWatch,ExternalLinks,
FilmHeader,JustWatchAttribution}.tsx`, `api/{types,public,me,query-keys}.ts`,
`pages/Settings.tsx`, `docs/specs/NEU-1412-tabbed-calendar.md`, frontend `CONTEXT.md`.

---

## 1. What is wrong, and what changes

Tom's ticket makes one argument in three parts. **Backlotter is about upcoming films**: it
follows a film from its first announcement to the first day you can watch it at home, and
nothing after. Three surfaces currently say otherwise.

- **The calendar mixes theatrical and home dates** under one date heading. A reader looking
  for one is wading through the other.
- **Physical** (TMDB release type 5, disc) is a home-release bucket. Almost nobody waits for
  the disc, and the disc date routinely lands *after* the film is already streaming, so it is
  clutter on the calendar, the film page, the `.ics` feed, the slate and the feed cards.
- **The where-to-watch box** on the film page lists the film's *current* US carriers from a
  snapshot the provider poll rebuilds daily. It promises an accuracy across time the site does
  not provide (the poll stops at 365 days; films released before launch were never polled), and
  it is not the site's job even when accurate. The home-release facts the site *does* own are
  the first-observation `now_available` events, and those stay.

What this ticket is **not**: it does not change which films are tracked, the provider poll's
set or cadence, the `now_available` ledger or its cards, the `release_date` card for an upcoming
US digital date (D-26's surviving half, the one NEU-1532 made fire after release), or the
digest's structure.

### Facts that shaped the decisions

- Release types are plain ints with one source of meaning, `RELEASE_TYPE_BUCKETS`
  (`catalog/release_grade.py`). No enum, CHECK constraint or migration constrains them.
  `rebuild_release_dates` stores **every** TMDB type unfiltered, and the module docstring says
  why that is load-bearing: narrowing the *displayable* cut therefore needs no data change and
  cannot backfill or "un-card" anything — stored type-5 rows are simply no longer displayable
  on either side of the diff.
- **Rent, buy and stream are not release dates.** They are watch-provider monetization types
  (`flatrate` / `rent` / `buy`), observed after the fact. The only home date TMDB *announces*
  is type 4 "digital", which it uses for both a rent/buy debut and a streaming-service debut.
  So the "rent/buy/stream" calendar the ticket asks for can only be the US digital date; the
  observations stay events (the glossary already says: "streaming is observed, not announced").
- `now_available` already fires once per (film, monetization type) on first observation and
  never again, so "only the first streaming release" is already the behaviour. Rent and buy
  are separate ledger types; seen together they share one card, seen apart they card twice.
- `film_availability_current` has exactly one reader, `_where_to_watch`. The ledger
  (`availability_first_seen`) and `watch_provider` are what `now_available` and the
  `rerender_now_available_summaries` script read; neither touches the snapshot.
- A theatrical and a home move in one observation share one `release_date` card with two
  `subject_key` tokens and a two-line body (`sweep.release_events`). Existing prod cards with a
  `US:physical` token are therefore of two shapes: physical-only, and mixed.
- The frontend's calendar is `/calendar`, server-rendered anonymous, with two **state-held**
  tabs for entitled readers ("My films" / "All releases"; D-1412.1: never a URL). Rows arrive
  pre-ordered (date, then bucket rank, then popularity) and the frontend groups adjacent rows;
  it has no type filter and the API has none.
- TMDB's API carries no JustWatch URL. The only link it gives is to TMDB's own
  `/movie/{id}/watch` page, which is derivable from `tmdb_id` with no data dependency.

### Decided in the planning session (2026-10-08)

- **D-1542.1 The home view is upcoming US digital dates, nothing else.** The second calendar
  view holds the US type-4 governing date per film, upcoming-only, exactly as the theatrical
  view holds types 2 and 3. Observed availability (`now_available`) never appears on any
  calendar: the calendar lists announced dates, and listing a past observation would break the
  upcoming-only rule and the slate-equals-calendar contract (FB-26). *Rejected:* dated entries
  from `now_available` observations; dropping the home view (the ticket asked for a split).
- **D-1542.2 A calendar has a `kind`: `theatrical` or `home`.** `GET /calendar` and
  `GET /me/calendar` take `kind` as a query parameter. `theatrical` = types
  `THEATRICAL_RELEASE_TYPES` (2, 3); `home` = `HOME_RELEASE_TYPES` (4). The filter is applied
  **inside the governing CTE**, so `total` (distinct dates) and the date paging are per kind.
  **Omitted means both** — the pre-split response minus physical — so an older frontend during
  a deploy keeps a working calendar, and the `.ics` feed and the slate reuse the unfiltered
  builder (D-1542.4). The frontend always sends one. *Rejected:* two endpoints (one builder,
  one DTO, one component on the other side); a required parameter (deploy-order coupling for
  nothing).
- **D-1542.3 One in-page control, "In theaters" / "At home".** A segmented control under the
  Calendar heading, rendered for every reader (anonymous readers get it alone; entitled readers
  get it below the My films / All releases tab strip). It is component state like the tab
  (D-1412.1): never a URL, never remembered, opens on **In theaters** — the server-rendered
  document stays the anonymous theatrical calendar. The kind is **shared across the two tabs**:
  switching tab keeps the kind. A kind's panel mounts on first selection and then stays mounted
  and `hidden`, as the tab panels do (D-1412.2), so paging progress survives a switch and the
  SSR document costs nothing extra for a reader who never switches. The home view **omits the
  bucket heading** (`h5`): it has one bucket, and "Digital" under every date is noise; the
  theatrical view keeps Wide / Limited. *Rejected:* two routes (`/calendar/home`) — a second
  indexable address for a view, against D-1412.1's reasoning and the glossary's "the tab is a
  view, not a place"; four flat tabs (crowded on a phone, and the two axes are independent).
- **D-1542.4 The `.ics` feed and the slate stay unsplit; physical leaves both.** *(Amended
  2026-10-08 by NEU-1543: the slate is split by kind over a 7-day window; the `.ics` half
  stands.)* One
  subscription URL and one slate, each carrying theatrical and digital dates under their bucket
  labels (the slate's headings: Wide / Limited / Digital). The glossary's "the slate is the
  my-films calendar reproduced" holds as the **union of the two views**. A subscriber's
  physical events disappear from the feed on the next fetch — a subscribed client drops what a
  feed stops publishing, which is the behaviour wanted here. *Rejected:* a split slate (two
  sections for a 30-day list that rarely holds more than a handful of home dates); two
  subscription URLs (every subscriber re-subscribes for half their calendar).
- **D-1542.5 Physical leaves the displayable set, not the storage.**
  `HOME_RELEASE_TYPES = {4}`, `RELEASE_TYPE_BUCKETS = {2: limited, 3: wide, 4: digital}`, and
  everything derived from them follows (labels, calendar order, `.ics` suffixes, slate tokens).
  `catalog.film_release_date` keeps storing every type (the load-bearing unfiltered insert);
  `film_release_date_change` rows of type 5 already written stay as history. No schema change
  for release dates. One guard is added: the sweep's release-date carder reads only moves whose
  type is in `RELEASE_TYPE_BUCKETS`, so a type-5 change recorded in the days before deploy and
  not yet carded is never carded as `US:5`.
- **D-1542.6 The physical cards already raised are repaired by a dry-run script** (D-1532.7's
  pattern). A `release_date` card whose `subject_key` is physical-only is **deleted** (its
  digest `notification` rows cascade; the audit prints them first). A **mixed** card loses its
  `US:physical` token and its body is **re-rendered** from the persisted change rows for the
  surviving tokens, through the same deterministic writer the sweep uses. A mixed card whose
  summary was hand-edited keeps its body, loses the token, and is counted. *Rejected:* hiding
  at read time (every reader grows a type check, and the body still says "physical" in the
  DB); leaving them (the ticket says "anywhere else it surfaces").
- **D-1542.7 The where-to-watch box is removed down to its table.** `where_to_watch` leaves
  the film DTO, `_where_to_watch` and the two DTO models go, the provider poll stops rebuilding
  the snapshot, and a migration drops `catalog.film_availability_current`. The ledger,
  `watch_provider`, `_upsert_providers`, `now_available` carding and the re-render script are
  untouched. **JustWatch attribution stays** wherever a `now_available` card names a service
  (DC-17, NR-8): that is TMDB's condition on the data, and the data still renders. Nothing is
  kept "for later". *Rejected:* keeping the snapshot written but unread.
- **D-1542.8 A "Where to watch (TMDB)" chip replaces the box, once the film is out.** A third
  external-link chip beside IMDb and TMDB, to `https://www.themoviedb.org/movie/{tmdb_id}/watch?locale=US`,
  rendered only when the film has a **US displayable release date on or before today**
  (theatrical or digital — a `release_dates` row with `country == "US"`, a non-empty
  `type_label`, and a date not after today). Computed in the route **loader**, not in render,
  so SSR and hydration agree. It hosts no provider data, so it carries no attribution.
  *Rejected:* no link at all (Tom wants the pointer); a JustWatch search URL (no stable id);
  always rendering it (an unreleased film's TMDB watch page is empty); gating on a
  `now_available` event (needs a new DTO flag).
- **D-1542.9 Rent and buy keep carding separately.** The ticket reads "rent/buy" as one type,
  but the observed behaviour — one shared card when seen together, two when seen apart — stays
  as it is. No ledger, CHECK, token or summary change. *Rejected:* "one store card per film"
  (suppress the second type's card) and a merged `store` monetization type.

---

## 2. Vocabulary (apply to `CONTEXT.md` in the implementation PR — see §4)

- **Calendar kind** — which of the two calendars a reader is looking at: **In theaters** (the
  US theatrical arc, wide and limited) or **At home** (the US digital date). The kind cuts
  across the calendar tab: My films and All releases each have both kinds. It is a view, not a
  place (no URL) and not a preference (not remembered). *Avoid:* filter, mode, category,
  "rent/buy/stream view" (the ticket's phrase; the view holds one announced date, not three
  observations), "digital tab" (it is not a tab).
- **Home-release date** — now **the US digital (TMDB type 4) release date**, singular. The
  physical date (type 5) is stored and never displayed, carded or listed. *Avoid:* physical
  release, disc date, Blu-ray date.
- **Where to watch** — retired. Was D-29's film-page box of current carriers. The phrase
  survives only as the label of the chip that links out to TMDB's watch page. *Avoid:*
  describing anything on the site as "where to watch" data; providers box; availability box.

---

## 3. Acceptance — backend

### 3.1 The displayable set (`catalog/release_grade.py`, `public/release.py`)

- `HOME_RELEASE_TYPES == frozenset({4})`; `RELEASE_TYPE_BUCKETS == {2: "limited", 3: "wide",
  4: "digital"}`; `release_bucket(5) is None`; `is_displayable_release(iso_3166_1="US",
  release_type=5, …) is False`. Module docstring's "4 and 5" and "US only (D-26)" prose
  updated to say digital alone and cite this ticket.
- `public.release.RELEASE_BUCKETS == ("limited", "wide", "digital")`;
  `RELEASE_BUCKET_LABELS` has no `physical`; `release_label_for_tmdb_type(5) is None` (or
  whatever the current "not displayable" return is — keep it).
- `rebuild_release_dates` is **unchanged**: type-5 rows are still stored.
- Docstrings and comments that enumerate "digital and physical" are brought to "digital":
  `catalog/queries.py:87-90`, `catalog/headline_release.py:23-25`, `catalog/models.py:666-670`,
  `ingest/release_dates.py:1-10`, `ingest/sweep/release_events.py:7,26-32`,
  `pipeline_run.py:464-465`, `public/dto.py:462`, `public/service.py` (`_calendar_governing_cte`,
  `_calendar_page`'s ordering comment, `get_ical_feed`), `synthesize/deterministic.py:59`,
  `app/services/digest_sender.py` (`_bucket_rank`), `docs/adr/0014-…` addendum wording.
- Tests: `tests/unit/public/test_release.py` — `test_release_buckets_constant`,
  `test_release_bucket_labels`, `test_every_bucket_has_a_label`, `test_tmdb_type_to_bucket_keys`
  (now `(2, 3, 4)`), `test_bucket_for_tmdb_type_physical` becomes "type 5 has no bucket".
  `tests/unit/ingest/tmdb/test_release_date_history.py` — `test_us_home_release_is_displayable`
  loops over `(4,)`; add `test_a_us_physical_date_is_not_displayable`;
  `test_origin_country_home_release_is_not_displayable` loops over `(4, 5)` still (both must
  be false). `tests/unit/synthesize/test_deterministic.py::test_home_release_date_slip_uses_the_same_verb`
  uses label `digital`.

### 3.2 The calendar kind (`public/service.py`, `routers/public.py`, `routers/me_calendar.py`, `public/dto.py`)

- `CalendarKind = Literal["theatrical", "home"]` in `public/dto.py` (exported; the routers and
  service import it). `CALENDAR_KIND_TYPES: dict[CalendarKind, frozenset[int]] =
  {"theatrical": THEATRICAL_RELEASE_TYPES, "home": HOME_RELEASE_TYPES}` beside it or in
  `service.py` — one spelling, derived from `release_grade`, never literal ints.
- `_calendar_governing_cte(*, name, title_follow_user_id=None, release_types=None)`: when
  `release_types` is given, the type filter is `release_type IN release_types` instead of
  `RELEASE_TYPE_BUCKETS`' keys. `get_calendar(session, *, kind: CalendarKind | None, limit,
  offset)` and `get_my_films_calendar(…, kind: CalendarKind | None, …)` pass
  `CALENDAR_KIND_TYPES[kind]` when `kind` is not None. `_calendar_page`, `_my_films_visible`,
  `load_slate` and `get_ical_feed` are untouched in behaviour (the last two never pass a kind).
- `_CALENDAR_BUCKET_ORDER == ("wide", "limited", "digital")`; `_CALENDAR_TYPE_RANK` follows.
- Routers: `kind: CalendarKind | None = Query(default=None)` on both routes, documented as
  "omitted = both kinds, for older clients". An unknown value is FastAPI's 422.
- `CalendarItem.release_type`'s comment lists three buckets. `CalendarResponse` is unchanged
  (no `kind` echo — the client asked for it, it knows).
- Tests, `tests/integration/routers/test_public_calendar.py`:
  - `test_calendar_type_mapping` maps 2/3/4 only; add `test_a_us_physical_date_is_not_on_the_calendar`
    (a film with only a US type-5 date yields no items with `kind` omitted or either kind);
  - `test_calendar_ordering` asserts wide, limited, digital;
  - new `test_kind_theatrical_lists_only_the_theatrical_arc` and
    `test_kind_home_lists_only_the_digital_date` — a film with US wide and US digital dates on
    different days appears once in each kind, and `total` counts that kind's dates only;
  - new `test_kind_omitted_lists_both_kinds` (today's shape minus physical);
  - new `test_an_unknown_kind_is_422`.
  `tests/integration/routers/test_me_calendar.py`: the same four `kind` tests against
  `/me/calendar` on a title-followed film; `test_the_dates_are_the_ics_feeds_dates` drops its
  type-5 fixture row or asserts it is absent from both; `test_ordering_within_a_date_is_the_public_routes`
  updated to three buckets.

### 3.3 The `.ics` feed (`public/service.py::get_ical_feed`, `public/ical.py`)

- `get_ical_feed` already derives its types from `RELEASE_TYPE_BUCKETS`; no code change beyond
  the docstring. `BUCKET_SUMMARY_SUFFIX` loses `physical`. UIDs for the surviving buckets are
  unchanged, so no subscriber's theatrical or digital event duplicates.
- Tests: `tests/unit/public/test_ical.py::test_the_summary_names_the_bucket` drops the
  `("physical", …)` case; `tests/integration/routers/test_public_ical.py::test_every_displayable_bucket_becomes_an_event`
  drops the type-5 case and gains `test_a_us_physical_date_becomes_no_event`.

### 3.4 The sweep's release-date carder (`ingest/sweep/release_events.py`)

- `load_release_change_backlog` adds `FilmReleaseDateChange.release_type.in_(tuple(RELEASE_TYPE_BUCKETS))`
  to its WHERE. The `or str(m.release_type)` fallbacks in `subject_keys` and `render_change`
  stay as defensive code but are now unreachable from this loader.
- `ReleaseDateGroup.region` and `subject_keys` are otherwise unchanged.
- Tests, `tests/integration/ingest/sweep/test_release_events.py`:
  `test_a_theatrical_and_a_home_date_in_one_observation_share_one_card` uses a US digital row
  and asserts `["US:digital", "US:wide"]` (or the current ordering); add
  `test_a_recorded_physical_change_is_never_carded` — a pre-existing `film_release_date_change`
  row of type 5 inside the window produces no event and no `US:5` token, and a row set with a
  type-5 and a type-3 move at one `changed_at` cards the wide move alone.

### 3.5 Where to watch is removed (`public/service.py`, `public/dto.py`, `ingest/providers.py`, `catalog/models.py`, migration)

- `FilmDetailResponse.where_to_watch`, `WhereToWatchOut`, `ProviderOut` and `_where_to_watch`
  are deleted. `get_film_detail` no longer awaits it. `PRIMARY_REGION`'s import in `service.py`
  stays if anything else uses it; otherwise goes.
- `ingest/providers.py`: `_rebuild_current` and its call are deleted; the `link` TMDB returns
  per region is no longer stored anywhere (the schema may keep parsing it). `Offer`,
  `_upsert_providers`, `_ledger_types`, `_insert_first_seen`, `_card_now_available` and the
  poll set are unchanged. The module docstring's "the box renders whatever is stored" line in
  `_upsert_providers` is reworded: the names are held for the `now_available` body and the
  re-render script.
- `FilmAvailabilityCurrent` is removed from `catalog/models.py`. A new Alembic migration
  (`drop_film_availability_current_neu_1542`) does `op.drop_table("film_availability_current",
  schema="catalog")` in `upgrade` and recreates the table, its unique constraint, its CHECK
  and its index in `downgrade`, copied from `7a45131257b9` (empty on downgrade — the poll no
  longer fills it, which the downgrade docstring says). `ck_ingest_run_kind` keeps `providers`.
  Run `alembic check` / the migration test as the repo does it (NEU-1507's in-process runner).
- `MONETIZATION_TYPES` and the ledger's CHECK are untouched.
- Tests: delete the seven `test_detail_where_to_watch_*` / `test_detail_exposes_where_to_watch_by_monetization_type`
  tests in `tests/integration/routers/test_public_films.py` and the `add_availability` fixture
  in `tests/fixtures/public.py` if nothing else uses it; add
  `test_detail_has_no_where_to_watch_key`. In `tests/integration/ingest/test_providers.py`,
  delete the snapshot tests (`test_the_snapshot_is_rebuilt_and_the_ledger_is_not_disturbed`,
  `test_a_film_nobody_carries_empties_its_snapshot`, `test_only_the_polled_region_is_rebuilt`,
  `test_the_snapshot_is_written_in_the_order_tmdb_listed_the_services`) and trim
  `test_writes_the_ledger_the_snapshot_and_the_providers` to the ledger and the providers; every
  `now_available` carding test stays green unchanged.
- `test_detail_exposes_displayable_release_dates` (`test_public_films.py:251-303`): the type-5
  row no longer appears; assert it is absent.

### 3.6 The prune script (`scripts/prune_physical_release_cards.py`, `tests/integration/scripts/test_prune_physical_release_cards.py`)

Shape and tone of `prune_released_trailer_cards.py` (module docstring: what happened, what it
removes, the audit, how to run), `--apply` off by default.

- **Selection:** `news.event` rows with `provenance = 'catalog'`, `event_type = 'release_date'`,
  and `subject_key && ARRAY['US:physical']`. Story-provenance cards have no subject and are
  never touched.
- **Physical-only** (every token is `US:physical`): delete the event (its `event_summary`,
  `event_story` and `app.notification` rows cascade or are deleted alongside, as the trailer
  prune does).
- **Mixed:** set `subject_key` to the tokens minus `US:physical`. If the summary is not
  hand-edited (`edited_at IS NULL`, deterministic model), rebuild the body: load the
  `film_release_date_change` rows for `(film_id, changed_at == event.occurred_at, iso_3166_1
  = US)` whose type is in `RELEASE_TYPE_BUCKETS`, render them through
  `release_events.render_change` → `ReleaseDatesChanged` → `render_summary` (the same path the
  sweep writes with), and update the summary row. If the change rows cannot be found, leave the
  body, strip the token, and count it. A hand-edited body is left and counted.
- **Audit** printed before any write: per card — film title, `occurred_at`, the tokens before
  and after, the body before and after (or "deleted"), the digest rows it earned (user, status,
  sent). Totals: deleted, re-rendered, token-only, skipped (edited / no change rows).
- Tests: physical-only deleted with its notification; mixed card re-rendered with the wide line
  only and the token gone; hand-edited mixed card keeps its body; story card untouched;
  dry run writes nothing; idempotent second run finds nothing.
- **Owed after deploy (Tom):** `python scripts/prune_physical_release_cards.py` dry run, then
  `--apply`, in the prod container. Until then old cards keep the word.

### 3.7 Digest (`app/services/digest_sender.py`, templates)

- No behaviour change beyond D-1542.5's derivations: `load_slate_markers`' tokens come from
  `_CALENDAR_BUCKET_ORDER` (now three), `_bucket_rank` follows, `SlateBucket.label` reads the
  three labels. Templates untouched (they render `bucket.label`).
- Tests: `tests/unit/app/test_digest_ranking.py::test_the_slate_groups_by_date_then_bucket_in_the_calendars_order`
  drops physical; `tests/integration/app/test_digest_sender.py` — the `record_date_change`
  fixture's `5: "physical"` entry goes, `test_the_slate_is_the_governing_us_date_per_release_type_inside_the_window`
  and `test_the_slate_is_the_my_films_calendar_over_its_window` lose their type-5 rows and
  assert a type-5 row contributes nothing.

### 3.8 Docs in this repo (same PR)

- `CONTEXT.md`: **Home-release date** rewritten per §2; **Slate** reads "theatrical and
  digital"; **My films calendar** gains a sentence that it has two kinds and the `.ics` feed is
  their union; new **Calendar kind** entry per §2 (the frontend file adds its own presentation
  notes); **Now-available event** unchanged; a **Where to watch** (retired) line under the
  nearest existing retired-terms pattern.
- `docs/specs/bl-consumer-pivot-project-spec.md`: D-26 *(amended 2026-10-08 by NEU-1542:
  physical dropped; the home release is digital alone)*, D-29 *(superseded 2026-10-08 by
  ADR-0023 / NEU-1542: the box is removed; a link to TMDB's watch page replaces it once the
  film is out)*, D-34 *(amended …: theatrical and digital)*, and the M6 contract line
  "`bucket ∈ premiere|limited|wide|digital|physical`" annotated. `bl-digest-content-project-spec.md`
  DC-9 *(amended …: three displayable buckets)*.
- `docs/adr/0023-the-site-tracks-a-film-to-its-first-home-availability.md` is written with
  this spec (status accepted; implementation tracked here). `docs/adr/0014` addendum wording
  updated to "digital".
- `RELEASE_NOTES.md` as the repo's release process requires (not per PR).

---

## 4. Acceptance — frontend

In full in `frontend/docs/specs/NEU-1542-calendar-split-and-initial-release-only.md`. Summary of
the contract this repo must hold for it:

- `GET /calendar?kind=` and `GET /me/calendar?kind=` with `theatrical` | `home`; omitted =
  both. Paging and `total` are per kind.
- `CalendarItem.release_type ∈ limited | wide | digital`.
- `FilmDetailResponse` has no `where_to_watch`; `release_dates` keeps `country`,
  `release_type`, `type_label`, `date`, so the frontend can gate the TMDB watch chip on a US
  displayable date on or before today.
- `ReleaseDateOut.type_label ∈ Limited | Wide | Digital | ""` (the primary-date fallback row).

---

## 5. Deploy

- **Order is free.** Backend first: the old frontend sends no `kind` and gets both kinds minus
  physical in one list, as today; it ignores the missing `where_to_watch` (its type is
  optional). Frontend first: the new frontend's `kind` is an unknown query parameter to the old
  backend, which ignores it, so both views show both kinds until the backend lands — cosmetic,
  and physical rows render with the frontend's title-case fallback for the few hours.
- **The migration drops a table.** Downgrade recreates it empty. Nothing else in the deploy is
  irreversible.
- **After deploy, Tom runs** the prune script (§3.6), dry run first. Watch the first sweep's
  `release dates:` line for a `US:5` token (there must be none) and the first providers run
  for a `_rebuild_current` reference in logs (there must be none).
- No Coolify variable changes. `HEALTHCHECK_PROVIDERS_URL` and the providers slot are
  unchanged.

---

## 6. Out of scope / deferred

- Pruning the past-dated `release_date` cards and release-day companion cards NEU-1532 left
  (still unticketed, see that spec).
- The `link/cluster.py` prompt line that calls a `release_date` event "theatrical, streaming,
  or home-video release" — story-path wording that could still let a trade's Blu-ray story form
  a `release_date` event. Changing LLM prompt wording is its own ticket.
- `CALENDAR_REGION` vs `PRIMARY_REGION` (two constants, one rule) — unchanged; the kind filter
  is a type filter and does not touch region.
- Remembering the kind or the tab across visits; putting either in the URL.
- A per-user "theaters only" digest or `.ics` (D-1542.4 says one of each).
- Any change to the provider poll's set, schedule or the `now_available` grain (D-1542.9).
- Backfilling or re-carding anything: narrowing the displayable set cards nothing by
  construction (`release_grade.py` docstring), and the one guard in §3.4 is for the backlog.
