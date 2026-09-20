# NEU-1414 — Follows carry coverage; the watchlist is a query over follows, mutes and one store setting

**Target repo:** upcoming-movies-backend

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1414
**Story:** NEU-1413 *Follow subsumes the watchlist*
**Milestone:** M8 — Follow subsumes the watchlist (shared contracts live on the milestone)
**Blocks:** NEU-1405 (film page), NEU-1415 (watchlist, follows and settings pages)
**Decisions:** D-42 to D-45 in `bl-consumer-pivot-project-spec.md` (D-45 amended here), ADR-0018
(storage resolved here); D-11, D-27, D-31 to D-34, D-39, D-40 unchanged
**Deferred to:** NEU-1416 *Follow any person at any credit depth, from a person page*

## What to build and why

M8 collapses the two records a user maintained into one. A **follow** is the only thing a user
keeps; it feeds the timeline *and* alerts. A person follow carries a **coverage** that picks which
of that person's credits alert. The store preference is one per-user setting. The **watchlist**
survives as a word for the set of films a user's follows cover for alerts, minus the films they
have **muted**, and this ticket is the backend that makes that set real.

The planning session on 2026-09-20 settled the one thing the milestone left open, and found two
things the milestone had not seen:

1. **The watchlist is a query, never a table.** Today's derivation is insert-only: nothing
   removes a `watchlist_item` when a follow is deleted, and nothing could when a coverage
   narrows from `all` to `lead` or a film ages out. Materialising would have needed a
   reconciliation pass in both directions that has never existed, while the notify and digest
   passes already run the follow graph as SQL per user. So `app.watchlist_item` is dropped, and
   one query builder is what every surface reads.
2. **"In play" is the wrong window for alerts.** `in_play_clause` drops a film the day its
   release date passes, but `now_available` cards fire 14 to 200 days *after* theatrical
   release, and today's persisted rows are what keep those alerts landing. The computed set
   uses an **alert window** instead: the provider poll's own ceiling, so the follow stops
   covering a film exactly when the catalog stops looking for offers on it.
3. **A mute silences the film everywhere, the timeline included.** D-45 as first written kept
   a muted film in the timeline. Tom's reading of a mute is "not interested in this film", so
   D-45 and ADR-0018 are amended: the timeline and the digest's timeline section drop the
   film's events too, including events that only name a followed person on it.

### Ground truth from what shipped

- `app.follow (user_id, entity_type, entity_id, source, created_at)`, PK on the first three,
  `ck_follow_entity_type`, `ck_follow_source` (`manual | letterboxd_import | tmdb_import |
  derived`; `derived` is written by nothing). `app/follow_queries.py` holds the D-11 builders:
  `followed_tmdb_ids`, `followed_film_uuids`, `followed_film_ids(user_id, today,
  excluded_statuses)` and `events_naming_followed_people(user_id)`, each a `Select` with
  `correlate(None)` so the notify pass can run it per user from `pipeline_run`.
- `app/services/derivation_service.py` owns D-13's cut (`watchlist_credit_clause`: director or
  `credit_order < WATCHLIST_TOP_BILLED_ORDER = 3`), `derivable_film_ids`, `derive_for_user`,
  `derive_for_follow`; `ingest/sweep/derivation_phase.py` runs it per entitled user after the
  credits pass; `follow_service.follow(derive=True)` runs it inline for a new follow.
- `app.watchlist_item (user_id, film_id, source, alert_prefs TEXT[] default {stream},
  created_at)` with `ck_watchlist_item_source`, `ck_watchlist_item_alert_prefs`,
  `ix_watchlist_item_film_id`. `app.watchlist_dismissal (user_id, film_id, created_at)`.
- Five surfaces join `watchlist_item` directly: `routers/watchlist.py` (GET/POST/PATCH/DELETE),
  `public.service._calendar_governing_cte(watchlist_user_id=)` for `/me/calendar`,
  `public.service.get_ical_feed`, `notify_service.alert_event_ids` (plus `load_recipients`'s
  "follow or watchlist item" working set and `now_available_matches_prefs(subject_key,
  alert_prefs)`), and `digest_sender.load_slate` (plus the weekly `load_recipients`).
- `ingest/providers.py::poll_set_clause` rule 2 is `EXISTS follow(title = film) OR EXISTS
  watchlist_item(film)`. `ingest/videos.py` shares that set unchanged and says so.
- `ingest/imports/apply.py::apply_watchlist_film` writes a watchlist item then a title follow
  (`derive=False`), skips the item when a dismissal exists, and counts
  `Progress.watchlist_created`, which reaches `import_job.watchlist_created` and the frontend's
  onboarding progress line ("N watchlist films").
- `app.user_settings (user_id, digest_cadence, ical_token, created_at, updated_at)`, row created
  lazily on first read; `digest_sender.load_recipients` COALESCEs the cadence over an outer
  join because most users have no row. `UserSettingsUpdateRequest` requires `digest_cadence`.
- `catalog.film_credit` holds the **full** cast and crew of every film (`ingest/tmdb/upsert.py`
  loops over the whole payload); seed grade is a read-time cut. `in_play_clause(today,
  excluded_statuses)` is `release_date IS NULL OR release_date >= today` AND `status IS NULL OR
  status NOT IN excluded`. `PROVIDER_POLL_MAX_AGE_DAYS` (config, default 200) bounds the
  provider poll's theatrical rule.
- Tests touching the old model (13 files): `test_derivation_service`, `test_derivation_phase`,
  `test_watchlist`, `test_follows`, `test_me_calendar`, `test_public_ical`, `test_notify_pass`,
  `test_notify_decisions`, `test_digest_sender`, `test_letterboxd_runner`, `test_tmdb_runner`,
  `test_providers`, `test_videos`; plus `tests/unit/ingest/sweep/test_summary.py` for the
  sweep detail line.

## Design

### D-1414.1 — One query builder, two sets: covered and watchlist

`app/follow_queries.py` grows the M8 half of the module, beside the D-11 builders and in the
same shape (a `Select`, `correlate(None)`, no session):

- `lead_credit_clause()` — director, or cast with `credit_order IS NOT NULL AND credit_order <
  LEAD_TOP_BILLED_ORDER` (3). This is `derivation_service.watchlist_credit_clause` and
  `WATCHLIST_TOP_BILLED_ORDER`, moved here with their docstrings (the "not 5, not a candidate
  for unification" argument stays).
- `covered_film_ids(*, user_id, today, excluded_statuses, max_age_days, only=None)` —
  `SELECT film.id` for every film this user's follows cover for alerts (D-43):
  - **person**: `FilmCredit.person_id IN followed person ids` where the credit passes
    `lead_credit_clause()` for a follow with `coverage = 'lead'` or `seed_grade_credit_clause()`
    for `coverage = 'all'`. The coverage is read from the follow row in the same subquery (join
    `Follow` to `FilmCredit` on the cast person id, `CASE`/`OR` on `Follow.coverage`), not fed
    in from Python, so the batch callers stay one statement per user.
  - **company**: `film_production_company`; **franchise**: `film.collection_id`; both as D-11.
  - The three branches above are ANDed with `alert_window_clause(...)` (D-1414.2).
  - **title**: the film itself, **any state**. The user asked for that film.
  - `only=(entity_type, entity_id)` narrows to one follow, as `derivable_film_ids` did; used by
    the want/stop service to ask "does anything *else* cover this film".
- `muted_film_ids(user_id)` — `SELECT film_id FROM watchlist_dismissal WHERE user_id = :u`.
- `watchlist_film_ids(...)` — `covered_film_ids(...)` minus `muted_film_ids`. **This is the
  watchlist.** `/me/calendar`, the iCal feed, the alert branch of the notify pass, the digest
  slate and the digest's weekly recipient rule read this and nothing else.
- `covering_follows(*, user_id, today, excluded_statuses, max_age_days)` — `SELECT film.id,
  follow.entity_type, follow.entity_id, follow.created_at` for every (film, follow) pair where
  the follow covers the film, the same branches as `covered_film_ids` but keeping the follow.
  `GET /me/watchlist` is built from this in one query: group by film in Python, `covered_by`
  in the D-1414.5 order, `followed` = a `title` pair exists, `created_at` = `min(created_at)`
  across the film's pairs, `muted` = film in `muted_film_ids`. Muted films are included.
- `covered_by_any_user_clause(*, today, excluded_statuses, max_age_days)` — a WHERE predicate
  over `catalog.film` for the provider and video polls (D-1414.3): `EXISTS` a follow that
  covers `Film.id` under the same branch rules whose user has no `watchlist_dismissal` row for
  the film. Spelled from the same per-branch helpers as `covered_film_ids`, so the two cannot
  disagree about what a follow reaches.

`followed_film_ids` and `events_naming_followed_people` both gain `AND film_id NOT IN
muted_film_ids(user_id)` **inside the builder** (D-1414.4), so every D-11 consumer (the
timeline, `digest_event_ids`) honours a mute without a second call site.

`derivation_service.py` and `ingest/sweep/derivation_phase.py` are deleted, with their tests.
The sweep's phase list, its summary/detail line (`tests/unit/ingest/sweep/test_summary.py`) and
any AGENTS.md mention of the derivation phase lose the `derived` counter.

### D-1414.2 — The alert window

`catalog/queries.py` gains `alert_window_clause(*, today, excluded_statuses, max_age_days)`
beside `in_play_clause`: `(release_date IS NULL OR release_date >= today - max_age_days) AND
(status IS NULL OR status NOT IN excluded)`. `max_age_days` is `settings.provider_poll_max_age_days`
at every call site; request-time entry points resolve it themselves the way `derive_for_follow`
resolved `today` and the statuses, batch passes are handed it once per run. Glossary:
**Alert window** in `CONTEXT.md`.

Why this and not `in_play_clause`: a film that opened last month is still owed its
`now_available` beat, and the provider poll only produces one for films up to
`PROVIDER_POLL_MAX_AGE_DAYS` past their theatrical date (rule 1) or that somebody is waiting on
(rule 2). Using the poll's own ceiling makes the follow stop covering the film exactly when the
catalog stops looking. Why not no cut at all: a company follow would cover its whole back
catalogue, and through D-1414.3 the provider poll would poll all of it every day.

### D-1414.3 — The provider and video polls read the computed set

`poll_set_clause` rule 2 becomes `covered_by_any_user_clause(...)`, replacing both the
`followed` and the `watchlisted` `EXISTS`. `run_provider_poll` passes `today`, the excluded
statuses and `max_age_days` through; `ingest/videos.py` shares the set unchanged and its
module docstring is rewritten (its "reaches this set only where D-13 has already derived an
item" paragraph is no longer true; the concern it raises about a prolific director is answered
by `coverage = lead` being the default and the alert window bounding the rest).

### D-1414.4 — Mute hides the film from the timeline too (D-45 amended)

Both D-11 builders exclude `muted_film_ids(user_id)`, so `get_timeline`, `digest_event_ids`,
the notify pass's digest branch and the `/me/timeline` route drop a muted film's events,
including events that only *name* a followed person on it. `public.service.get_timeline`'s
docstring and the `/me/timeline` tests gain the case. Nothing else changes: a mute never
deletes a follow (D-40), and un-muting restores the film everywhere at once.

### D-1414.5 — `/me/watchlist`: want and stop

`routers/watchlist.py` and `watchlist_service.py` are rewritten over the builders:

- `GET` → `{items: [{film, covered_by: [{entity_type, entity_id, name}], followed, muted,
  created_at}]}`, newest `created_at` first, then title, then id. `source` and `alert_prefs`
  leave the payload. `covered_by` is ordered direct title follow first, then covering follows
  oldest first; `name` is resolved through `follow_repo.entity_labels` in one grouped lookup
  and is `null` for a follow the catalog cannot resolve (D-40, as `FollowOut`). `film` keeps
  `WatchlistFilmOut` with the headline release (NEU-1397), resolved for the whole list in one
  `headline_releases` call as today.
- `POST {film_id}` = **want**, always `200` with the item: delete any mute for the film; then if
  no follow covers the film (`covered_film_ids(only=None)` does not contain it), create a
  `manual` title follow. Idempotent: wanting a covered, unmuted film changes nothing and
  answers the item. `404 film_not_found` for an unknown film. A title follow covers any state,
  so wanting an old film yields an item.
- `DELETE /{film_id}` = **stop**: delete the direct title follow if one exists; then if any
  other follow still covers the film, write a mute (`ON CONFLICT DO NOTHING`, dated from the
  first) and answer `200` with the item (`muted: true`). If nothing covers it any more there is
  no item to answer: **`204`**. `404 film_not_found` for an unknown film. A film that nothing
  covers and that is not muted is also `404 watchlist_item_not_found`, as today: there is
  nothing to stop. Stop on an already-muted film is `200` with the muted item.
- `PATCH /{film_id}` is removed, with `WatchlistUpdateRequest`, `normalise_alert_prefs`'s
  per-item use and `watchlist_service.set_alert_prefs`.
- The `201`/`200` split on `POST` goes: both verbs answer the *resulting* state, which is what
  NEU-1405 reconciles its cache from.

`watchlist_repo.py` shrinks to `get_film`, `add_mute`, `delete_mute` (renamed from
`add_dismissal`) and the read helpers the service needs; `WatchlistItem` and its DTO fields go.

### D-1414.6 — Coverage on the follow

- Model and migration: `app.follow.coverage TEXT NOT NULL DEFAULT 'lead'`,
  `ck_follow_coverage` (`coverage IN ('lead', 'all')`) — written by hand in the migration and
  named identically on the model (AGENTS.md; the parity test compares names).
  `FOLLOW_COVERAGES = ("lead", "all")`, `DEFAULT_COVERAGE = "lead"` in `app/models.py`. The
  column is stored on every row; it is read only for `entity_type = 'person'`.
- `FollowCreateRequest.coverage: Literal["lead","all"] | None`; a `model_validator` rejects
  `coverage` on a non-person follow (`422`, detail `coverage_not_applicable`). Absent means
  the default.
- New `PATCH /me/follows/{entity_type}/{entity_id} {coverage}` (`FollowUpdateRequest`, field
  required), CSRF, `require_entitled()` via the router. `422 coverage_not_applicable` for a
  non-person, `404 follow_not_found` when there is no such follow, `200` with `FollowOut`.
  `follow_service.set_coverage` commits; `follow_repo.set_coverage` writes the loaded row.
- `FollowOut.coverage: str` echoes the stored value on every row (always `lead` for a
  non-person; NEU-1415 shows the control on person rows only).
- `follow_service.follow` loses `derive` and the derivation call; it takes `coverage: str |
  None` and writes `DEFAULT_COVERAGE` when none. A second follow of the same entity still
  returns the existing row untouched, coverage included: changing coverage is the PATCH's job.
- `unfollow` is a plain delete, never an implicit mute. The film stays on the watchlist if
  another follow covers it, and mutes survive (D-40).
- Widening the tiers later (NEU-1416) is a change to `FOLLOW_COVERAGES` and the CHECK only.

### D-1414.7 — One store setting per user

- `app.user_settings.alert_stores TEXT[] NOT NULL DEFAULT '{stream}'::text[]`,
  `ck_user_settings_alert_stores` (`alert_stores <@ ARRAY['buy','rent','stream']::text[]`).
  `ALERT_PREFS`/`DEFAULT_ALERT_PREFS` in `app/models.py` become `ALERT_STORES` /
  `DEFAULT_ALERT_STORES`. An empty array is allowed and means no store alerts.
- `UserSettingsOut.alert_stores: list[str]`. `UserSettingsUpdateRequest` makes
  `digest_cadence` optional, adds `alert_stores: list[AlertStore] | None`, and a
  `model_validator` requires at least one field (an empty PATCH stays a `422`).
  `alert_stores` is normalised (deduped, canonical order) by the validator that
  `normalise_alert_prefs` is today, renamed.
- `settings_service.update(db, *, user, digest_cadence=None, alert_stores=None)` replaces
  `set_digest_cadence` (which the rotate route does not use); `user_settings_repo` gains
  `set_alert_stores`. Both routes keep creating the row lazily on first touch.
- The notify pass reads the setting **without creating a row**: `load_recipients` outer-joins
  `UserSettings` and selects `COALESCE(alert_stores, DEFAULT)` as a column, the way the digest
  pass reads the cadence; `Recipient` gains `alert_stores`. `now_available_matches_prefs`
  becomes `now_available_matches_stores(subject_key, alert_stores)` with the same mapping
  (`ALERT_PREF_BY_MONETIZATION` renamed `ALERT_STORE_BY_MONETIZATION`) and the same
  `test_notify_decisions` pin against `MONETIZATION_TYPES`.

### D-1414.8 — The delivery passes over the computed set

- **Notify.** `load_recipients`'s working set is "any user with a follow" (the watchlist
  `EXISTS` goes). `alert_event_ids` selects whitelist events in the window whose
  `Event.film_id IN watchlist_film_ids(user)`, and filters `now_available` by the recipient's
  `alert_stores`. `digest_event_ids` is unchanged in code and gains the mute exclusion through
  the builders. D-39's suppression rule and its per-pass test are untouched.
- **Digest.** Weekly `load_recipients` is "queued digest row OR any follow". `load_slate` joins
  `FilmReleaseDate.film_id IN watchlist_film_ids(user)`. "Nothing queued and an empty slate
  gets no mail" already covers a user whose follows cover nothing dated.
- **Calendar and iCal.** `_calendar_governing_cte(watchlist_user_id=)` and `get_ical_feed`
  replace their `WatchlistItem` join with `FilmReleaseDate.film_id.in_(watchlist_film_ids(...))`.
  Their docstrings stop saying "a follow produces timeline rows and nothing else".

### D-1414.9 — Importers write a title follow per film

`apply_watchlist_film` writes the film in full and a title follow, nothing else. It no longer
reads `watchlist_dismissal`: an import re-listing a film the user has muted creates the follow
(they listed it) and **leaves the mute alone** (they silenced it; the film shows in
`GET /me/watchlist` as `muted: true` and can be un-muted). `Progress.watchlist_created` counts
the title follows written for watchlist rows, so `import_job.watchlist_created` and the
onboarding screen's "N watchlist films" keep their meaning; `follows_created` keeps counting
person follows. `follow_service.follow` no longer takes `derive`, so `follow_entity` drops it.
The runner and `tmdb_account` docstrings that explain the item-before-follow ordering are
rewritten.

### D-1414.10 — Migration, once

One Alembic revision, `task makemigration` for the column adds and drops, then hand-edited:

1. `ALTER TABLE app.follow ADD COLUMN coverage TEXT NOT NULL DEFAULT 'lead'` +
   `ck_follow_coverage`.
2. `ALTER TABLE app.user_settings ADD COLUMN alert_stores TEXT[] NOT NULL DEFAULT
   '{stream}'::text[]` + `ck_user_settings_alert_stores`.
3. `INSERT INTO app.follow (user_id, entity_type, entity_id, source, coverage, created_at)
   SELECT user_id, 'title', film_id::text, source, 'lead', created_at FROM app.watchlist_item
   WHERE source <> 'derived_from_follow'` `ON CONFLICT DO NOTHING` — every row a user or their
   import chose becomes a title follow with its own `source` and `created_at`; an existing
   follow wins. `derived_from_follow` rows are not copied: their follows recompute them.
   Per-item `alert_prefs` are not carried anywhere; the user's setting starts at `{stream}`.
4. `DROP TABLE app.watchlist_item` (index and constraints with it). `watchlist_dismissal` is
   untouched.

`downgrade()` drops the two columns and recreates an empty `watchlist_item`; the copied rows
are not un-copied and the migration says so in its docstring. `test_migrations.py`'s parity
check passes with the two hand-named constraints on the models.

### What does not change

- D-11 timeline coverage for every follow (apart from the mute exclusion). `coverage` never
  widens or narrows the timeline.
- D-39 gating on every `/me/*` route and in both batch passes; D-40 (nothing here deletes a
  follow, a mute or a settings row on lapse; the migration deletes only `watchlist_item`).
- `followed_tmdb_ids`'s digit guard and `followed_film_uuids`'s UUID guard, which every new
  builder reuses.
- `FOLLOW_SOURCES` (the unused `derived` value stays; removing it is not this ticket).
- `/me/follows` GET and DELETE, `/people/*`, `/companies/*`, `/collections/*`.
- The push whitelist (D-32), the digest cadence, the iCal token, web push.

## Acceptance criteria

### `app/follow_queries.py`, `catalog/queries.py`

- `covered_film_ids` returns, for a person follow at `lead`, films where the person is director
  or top-3 billed and inside the alert window; at `all`, every seed-grade credit; never a
  4th-billed credit at `lead`, never a 6th-billed credit at `all`.
- Company and franchise follows cover matching films inside the alert window only; a title
  follow covers its film released, cancelled or undated.
- A film whose primary date is `today - max_age_days` is covered; one a day older is not; one
  with a NULL date is covered; one whose status is excluded is not, whatever its date.
- `watchlist_film_ids` = `covered_film_ids` minus mutes; `covering_follows` lists every
  (film, follow) pair; `covered_by_any_user_clause` is true for a film covered by *any* user
  who has not muted it and false when the only covering user has.
- `followed_film_ids` and `events_naming_followed_people` exclude a muted film's rows.

### `routers/watchlist.py`, `watchlist_service.py`

- `GET` answers the M8 shape with muted films included, direct-title-first `covered_by`,
  `followed`, `muted`, `created_at = min` over covering follows, newest first; no `source`, no
  `alert_prefs`.
- `POST`: un-muting, creating a `manual` title follow when nothing else covers, idempotent on
  a covered film; always `200` with the item; `404` for an unknown film.
- `DELETE`: deletes a direct title follow; mutes and answers `200` when another follow still
  covers; `204` when nothing does; `404` for an unknown or uncovered, unmuted film; `200` on
  an already-muted film.
- No `PATCH` route.

### `routers/follows.py`, `follow_service.py`, `app/dto.py`, `app/models.py`

- `POST /me/follows` accepts `coverage` for a person, `422 coverage_not_applicable` for the
  other types, defaults to `lead`; `FollowOut.coverage` present on every row.
- `PATCH /me/follows/{type}/{id}` sets a person follow's coverage, `422` for a non-person,
  `404` for a missing follow, CSRF-protected, entitlement-gated.
- `Follow.coverage` mapped with `ck_follow_coverage`; `WatchlistItem` removed from the models
  and `tests/unit/test_model_registration.py`.

### `routers/user_settings.py`, `settings_service.py`, `user_settings_repo.py`

- `GET /me/settings` carries `alert_stores` (default `["stream"]`); `PATCH` accepts either or
  both fields, rejects an empty body, normalises and accepts `[]`.
- `UserSettings.alert_stores` mapped with `ck_user_settings_alert_stores`.

### `notify_service.py`, `digest_sender.py`, `public/service.py`, `ingest/providers.py`

- Notify: a person follow at `lead` alerts on the director's film and not on a 4th-billed one;
  at `all` on every seed-grade credit; a `now_available` card alerts only when a token's store
  is in the user's `alert_stores` (default `{stream}`, no settings row needed); a muted film
  alerts nothing and its events leave the digest branch too. Suppression test per pass unchanged.
- Digest weekly slate lists the computed set's dates and not a muted film's; weekly recipients
  include a user with a follow and no queued row.
- `/me/calendar` and the `.ics` feed agree with `GET /me/watchlist` minus muted films, and a
  director follow puts that director's in-window films on both (rewrite the "following a
  director puts nothing here" assertions).
- `/me/timeline` omits a muted film's events, including a mention-only event.
- Provider and video poll sets include a film covered only through a person follow inside the
  window and exclude one every covering user has muted.

### `ingest/imports/apply.py`, runners

- A Letterboxd or TMDB import writes one title follow per watchlist film and no other row;
  `watchlist_created` counts them; a muted film gets its follow and keeps its mute.

### Migration

- On a database holding manual, imported, derived rows and a dismissal: manual and imported
  rows without a follow become title follows with their `source` and `created_at`; rows with a
  follow already are left alone; derived rows vanish; the dismissal survives; the table is gone;
  `follow.coverage` and `user_settings.alert_stores` exist with their CHECKs.

### Tooling

`task format`, then `task test && task lint && task typecheck` green. The suite runs in the
api container against the single-writer `app_test` database, so run it once, in the foreground.

## Out of scope / deferred

- **NEU-1416** — following any person at any credit depth (a third coverage tier, what the
  sweep enumerates and the credit history cards for followed non-seed people, a person page).
  This ticket only promises that widening `FOLLOW_COVERAGES` is a constraint change.
- **Frontend** (NEU-1405, NEU-1415). Two contract details settled here that those specs should
  pick up: `DELETE /me/watchlist/{film_id}` answers `204` once nothing covers the film, and
  `source` leaves the watchlist item payload (NEU-1405's `MyWatchlist.tsx` reads `item.source`
  for its removal confirm; `undefined` there is harmless, and NEU-1415 deletes the confirm).
- Removing the unused `derived` value from `FOLLOW_SOURCES`.
- Tuning `PROVIDER_POLL_MAX_AGE_DAYS`: the alert window rides on it by design; changing it is a
  provider-poll decision.
- A CONTEXT.md sweep for every docstring that still says a follow produces timeline rows only;
  the ones on the surfaces this ticket rewrites are in scope, the rest are not.
