# NEU-1370 — Sanity holds with an ingest.credit_hold log

**Project:** bl: Consumer Pivot · **Milestone:** M5 · **Story:** NEU-1333
**Project spec:** `docs/specs/bl-consumer-pivot-project-spec.md` (D-8) · **Blocked by:** NEU-1368

## Problem

The quarantine window (NEU-1368) suppresses edits that get reverted quickly. Two shapes of
defacement survive a time window: a vandal attaching one person to dozens of films in a day
(each attachment looks ordinary on its own), and a credit that is impossible on its face — a
person dead for years attached to a new project, or an infant billed as a lead. These need
**holds** that look at the person, not the clock, and they must **hold, not discard**: a hold
that turns out to be real (a prolific documentary producer, a posthumous release) must still
publish once the condition clears or a human confirms it.

The person table carries no birth or death dates, and the sweep only ever calls
`/person/{id}/movie_credits`, which does not return them. Decided (2026-09-15): fetch
`/person/{id}` **lazily, once, only for people about to be carded** — cost is one call per newly
carded person, never per seed person.

## What to build

### 1. Person details, lazily

- `catalog.person` gains `birthday DATE NULL`, `deathday DATE NULL`,
  `details_observed_at TIMESTAMPTZ NULL`. Migration; model first.
- `TMDBClient.person_details(person_id) -> TMDBPersonDetails` (`/person/{id}`; fields `id,
  name, birthday, deathday, popularity, profile_path, known_for_department`). 404 →
  `mark_person_missing` as elsewhere.
- `ensure_person_details(session, client, person_id)` in `ingest/tmdb/upsert.py`: no-op when
  `details_observed_at` is set; otherwise fetch, write the three columns plus a refreshed
  `popularity`/`profile_path`, and stamp `details_observed_at`. Never refreshed afterwards
  (a birthday does not change; a death is rare and tolerable to miss until a manual refresh).

### 2. The hold log

- `ingest.credit_hold (id, film_id, person_id, credit_type, changed_at, reason ∈
  burst|deceased|implausible_age, held_at, released_at NULL, release_reason ∈
  cleared|expired|manual NULL)`, unique on `(film_id, person_id, credit_type, changed_at)`.
  Registered in `upmovies/models.py`; migration.
- A row is **open** while `released_at IS NULL`. The attachment loader
  (`load_attachment_backlog`) excludes changes with an open hold; it re-includes them once
  released with `cleared` or `manual`. `expired` rows are never re-included: the change is
  older than `SWEEP_EVENT_LOOKBACK_DAYS` and would not be read anyway.

### 3. Checks, run per attachment group before carding

Run inside the credits phase, after the quarantine gate and before `_card_group`, over the
credits still eligible:

- **Burst.** Count distinct films with an `added` row for this person whose `changed_at` falls
  on the same UTC day as this credit's `changed_at`. If ≥ `SWEEP_SANITY_MAX_FILMS_PER_DAY`
  (default **20**), hold every one of those credits with `reason=burst`. Re-evaluated each
  pass: the hold **clears** when the count of those credits still present in `film_credit`
  drops below the threshold (TMDB reverted the burst) — the survivors then card normally.
- **Deceased.** After `ensure_person_details`: if `deathday` is set and
  `deathday < changed_at - SWEEP_SANITY_POSTHUMOUS_YEARS` (default **2** years), hold with
  `reason=deceased`. Posthumous credits inside two years are normal (completed films,
  archive footage) and are not held.
- **Implausible age.** If `birthday` is set and the person is younger than
  `SWEEP_SANITY_MIN_AGE_YEARS` (default **3**) at `changed_at`, hold with
  `reason=implausible_age`. Applies to every seed-grade role.
- `deceased` and `implausible_age` holds do not clear on their own (the facts do not change);
  they **expire** when the change ages out of the lookback window, or are released manually.

### 4. Manual release and visibility

- `POST /admin/credit-holds/{id}/release` (`require_current_admin` + CSRF) sets
  `released_at`, `release_reason=manual`; the next sweep pass cards it (still subject to the
  ordinary quarantine/suppression rules). `GET /admin/credit-holds?open=true` lists open holds
  with person, film, reason, `changed_at`. Frontend page is **not** in this ticket; the JSON is
  enough to unblock a real hold from the admin token until a page exists.
- Sweep detail line gains `holds: N new, M cleared, K expired`.
- Every hold and release logs at INFO with film, person, reason.

### 5. Settings

`SWEEP_SANITY_MAX_FILMS_PER_DAY=20`, `SWEEP_SANITY_POSTHUMOUS_YEARS=2`,
`SWEEP_SANITY_MIN_AGE_YEARS=3`, all validated > 0 at startup; added to
`docker-compose.prod.yml` and the AGENTS.md deploy checklist.

## Acceptance criteria

- A fixture where one person is added to 25 films on one day: all 25 attachments are held
  with `reason=burst`, no events are carded; after 20 of them disappear from `film_credit`,
  the next pass clears the holds and cards the remaining 5.
- A person with `deathday` 10 years before the attachment is held `deceased`; one who died
  last year is carded.
- A person born 1 year before the attachment is held `implausible_age`; a 12-year-old cast
  member is carded.
- `ensure_person_details` fetches exactly once per person across two passes (respx call
  count), and never for people who are not about to be carded.
- A manually released hold cards on the next pass; an expired one never does.
- Open holds are excluded from the attachment backlog; released ones are not.
- Pre-existing quarantine (NEU-1368) and removal-dwell (NEU-1205) behaviour unchanged (their
  tests still pass).
- `task format`, then `task test && task lint && task typecheck` pass.

## Out of scope

- Backfilling `birthday`/`deathday` for all seed people (rejected; lazy only).
- An admin UI page for holds (JSON endpoints only here).
- Holds on release-date or status changes.
- Defacement-magnet-title tuning (variable quarantine bar) — waits on the NEU-1372 spike.
