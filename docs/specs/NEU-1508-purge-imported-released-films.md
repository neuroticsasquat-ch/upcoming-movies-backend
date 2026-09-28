# NEU-1508 — Purge the long-released films that pre-NEU-1505 imports put in the catalog

**Target repo:** upcoming-movies-backend only. The frontend needs nothing: a purged film's page
already 404s, and the sitemap and search drop it on their own (§3.4).

**Linear:** https://linear.app/neuroticsasquatch/issue/NEU-1508 (Bug, Medium)
**Project:** bl: Maintenance (no milestone, no project spec)
**Related:** NEU-1505 (D-1505.7 is reversed here; D-1505.6 is what keeps them out), NEU-1436
(the admission exception that carded them), NEU-1449 (the import path that admitted them),
ADR-0018 D-40 ("nothing deletes user graph rows" — the follow half is an exception here)
**Ground truth read (2026-09-28):** `catalog/models.py` (`Film`, `FilmFieldChange`, the
`film_field_change_trg` trigger), `app/models.py` (`Follow`, `ImportCandidate`),
`news/models.py` (`Story.film_id` SET NULL, `Event` CASCADE), every FK onto `catalog.film` in
`migrations/versions/`, `ingest/imports/apply.py` (`propose_film`, `film_id_for`,
`release_date_in_window`, `in_alert_window`), `ingest/tmdb/filters.py::is_unreleased`,
`public/service.py` (`get_film_detail`, `get_sitemap_films`, `get_film_search`),
`app/services/follow_service.py` + `app/repos/follow_repo.py` (`/me/follows` over an orphan),
`link/pipeline.py` + `link/retrieval/index.py` (what the linker can re-link),
`scripts/prune_admission_cards.py` + its test (the precedent), and the local database
(`catalog.film`, `film_field_change`, `app.follow`, `news.event`, `news.story` counts below).

---

## 1. What is wrong, and what changes

Before NEU-1505, an import (EF-21) upserted every film a user's watchlist named, whatever its
age. Films that had opened years earlier — *The Curious Case of Benjamin Button* (2008), *Mank*,
*The Killer*, *Beau Is Afraid* in prod, admitted 2026-09-23 by another user's Letterboxd import
— sit in `catalog.film` with public pages, and nothing about them belongs in an upcoming-films
catalog. NEU-1505 gated admission and declined out-of-window films before they are fetched,
and D-1505.7 chose to leave the ones already in. Tom's call (2026-09-28) reverses that: they
have no business in the database. The cards they earned were already removed on 2026-09-28
by `scripts/prune_admission_cards.py` (backend PR #390); this ticket removes the **films**.

Two things the code says that this ticket contradicts, and must amend rather than ignore:

- `Film.tmdb_missing_at`'s docstring: *"Films are never deleted (spec §4.4: 'if we announced
  it, we do not un-announce it')"*.
- `Follow`'s docstring: the catalog cannot cascade into `app.follow`, *"acceptable, because
  films are never deleted (spec §4.4)"*.

§4.4 stays the rule. Its premise is *announced*: a film the catalog admitted while upcoming was
announced to followers and stays, whatever it becomes. A film first observed already outside
the alert window was never announced — no card could have been right about it — and this is a
one-off removal of exactly those (D-1508.5), not a new lifecycle state.

### Decided in the planning session (2026-09-28)

- **D-1508.1 A candidate is a film that was outside the alert window on the day it was
  created, judged by the release date it had then.** Not "released before creation" (the
  ticket's literal wording): EF-21 admits a film released inside the window, and an import
  today would offer those again — locally, 45 films released before their creation date sit
  inside the window (e.g. *Frankenstein*, opened 2025-10-17, imported 2026-09-20) and are
  legitimate. And not "by today's date": TMDB back-dates films after we admit them. Locally 8
  sweep-admitted films arrived **undated** in August and were later given dates from 2017 to
  2025 (`film_field_change` rows with `field='release_date'`, `old_value` `null`); a film that
  was unreleased when first observed is exactly what the ticket says to keep. So the date the
  rule reads is the one at creation: the `old_value` of the earliest `release_date` change row
  on the film if one exists, else the current `release_date`. The window is
  `PROVIDER_POLL_MAX_AGE_DAYS` (365 in prod, NEU-1417), read from settings, not re-spelled.
- **D-1508.2 Title follows on a purged film are deleted, silently, and listed by `user_id` in
  the dry run.** `app.follow` has no FK to film (by design), so the alternative is a nameless
  row on `/me/follows` until the user unfollows it. The follow could never card again — the
  film is outside the window and cannot re-enter (§3.5) — so there is nothing to tell the user
  they lost. This is a one-off exception to D-40, scoped to the rows this script names.
- **D-1508.3 Stories stay; they are unlinked and marked `rejected`.** `news.story.film_id` is
  SET NULL, which would leave `link_status='linked'` on a row pointing at nothing. The script
  writes `film_id = NULL, link_status = 'rejected'` explicitly, before the film goes, so no
  row claims a link it does not have and the linker (which only revisits `pending`) never
  re-processes it. The article text is fetched, not ours to lose. Deleting stories was
  rejected; so was leaving the contradiction.
- **D-1508.4 Old URLs answer 404, and nothing is built for it.** `get_film_detail` returns
  `None` for an unknown ref and the router raises 404; the frontend film route throws its own
  404 on that. The sitemap lists only films with a visible summarised event, and search reads
  `catalog.film`, so both drop a purged film the moment the row is gone. A 410 would need a
  tombstone table, a route branch and a frontend error case for a handful of URLs.
- **D-1508.5 One-off, with creation bounds, like the two prune scripts.** Candidates must
  have been created between `IMPORTS_SHIPPED` (2026-09-17, NEU-1356's merge — nothing before
  it could be an import) and `--before` (default `DEFAULT_CUTOFF = 2026-09-28`, NEU-1505's
  deploy — nothing after it admits an out-of-window film). The bounds are what make the
  reconstruction in D-1508.1 safe: `film_field_change` has recorded `release_date` since
  2026-07-05, well before any candidate could exist. The docstrings gain an amendment, not a
  rewrite; no ADR changes.
- **D-1508.6 Events cascade, but the script deletes them explicitly, precedent-style.** Every
  FK onto `catalog.film` is `ON DELETE CASCADE` except `story.film_id`. The catalog children
  (credits, genres, companies, release dates, videos, availability, change history, the
  resolution cache, `import_candidate`, `credit_hold`, `link_retrieval_probe`) are left to the
  cascade: they are the film's own rows and carry nothing user-facing. Events are deleted
  explicitly — `event_story`, `event_summary`, then `event` — after the audit has read the
  `app.notification` rows that cascade with them, as `prune_admission_cards.py` does. There
  should be none left after the 2026-09-28 prune; the dry run says so or lists them.

### Measured locally (2026-09-28, not prod)

| Rule | Films |
|---|---|
| `release_date < created_at::date` | 450 |
| `release_date < created_at::date - 365` (today's date) | 405 |
| D-1508.1 (date at creation) | 397 |

The 397 carry 397 title follows (Tom's own 2026-09-20 test import), 0 events, 0 stories,
0 `import_candidate` rows (NEU-1505's migration deleted the unticked ones). Prod is smaller
and different: the script's dry run there is the measurement the ticket asks for, and the
auto-mode classifier refused a read-only query against prod during planning, so it has not
been made yet.

---

## 2. Acceptance

### The script (`scripts/purge_imported_released_films.py`)

- Dry run by default; `--apply` deletes; `--before` (ISO, UTC when no offset) overrides
  `DEFAULT_CUTOFF`. One `SessionLocal` session, `logging` not `print`, a testable
  `async def purge(session, *, apply: bool, before: datetime = DEFAULT_CUTOFF) -> Purged`,
  the audit printed from a dry pass before any `--apply` pass (the notification rows it reads
  cascade). `main(argv)` mirrors `prune_admission_cards.main`.
- **Selection** (`_candidates`): films with `IMPORTS_SHIPPED <= created_at < before` whose
  release date at creation (D-1508.1) is not NULL and is before
  `(created_at AT TIME ZONE 'UTC')::date - max_age_days`. Spelled once, in SQL, over
  `catalog.film` left-joined to the earliest `film_field_change` row with
  `field = 'release_date'` per film. The status half of `is_unreleased` is deliberately not
  consulted: a film with `Released` and no date at creation is undated, and undated films are
  the sweep's, not an import's.
- **Report, per film, in title order:** title, tmdb_id, current ref (`film_ref(tmdb_id,
  title)`), release date at creation, `created_at`; then each title follow's `user_id` and
  `source`; each linked story's id and title; each event's type and `occurred_at` with its
  notification rows (user_id, status, sent_at). Totals at the end: films, follows, stories,
  events. `user_id`s, never emails: the dry run is pasted on the PR.
- **Apply, in this order, one transaction:** `UPDATE news.story SET film_id = NULL,
  link_status = 'rejected' WHERE film_id IN (…)`; `DELETE event_story`, `event_summary`,
  `event` for the films' events; `DELETE app.follow WHERE entity_type = 'title' AND entity_id
  IN (…::text)`; `DELETE catalog.film WHERE id IN (…)`; commit. Cascade takes the rest.

### Tests (`tests/integration/scripts/test_purge_imported_released_films.py`)

Fixtures stamp every timestamp explicitly (the NEU-1121 lesson recorded in the precedent
test); `NOW = 2026-09-23` for the incident import, `IMPORTS_SHIPPED <= NOW < DEFAULT_CUTOFF`.

- **A pre-fix import film is purged.** A film created at `NOW` with `release_date =
  2008-12-25`, a title follow, a linked story, one `catalog` event with a summary and a
  notification. After `--apply`: the film, its follow, its event, summary and notification
  are gone; the story remains with `film_id NULL` and `link_status = 'rejected'`. The dry run
  reported the follow's `user_id`, the story and the event, and deleted nothing.
- **A film that was upcoming when first observed is untouched** (the ticket's acceptance):
  created at `NOW` with `release_date = 2026-09-30`, plus a `film_field_change` row
  `field='release_date', old_value="2026-09-30", new_value="2026-09-24", changed_at > NOW`
  and the current `release_date` set to `2026-09-24`. It is not a candidate. Neither is a
  film created at `NOW` with `release_date` NULL that later gained `old_value: null →
  "2017-03-11"` (the sweep case seen locally).
- **The window, not release day, is the line.** Created at `NOW`, `release_date =
  NOW - 300 days` → kept; `NOW - 366 days` → purged. Uses the configured
  `provider_poll_max_age_days`.
- **The bounds hold.** The same 2008 film created at `IMPORTS_SHIPPED - 1 day` → kept; created
  at `DEFAULT_CUTOFF` → kept; created at `DEFAULT_CUTOFF - 1 s` → purged.
- **The purged film's routes.** After `--apply`, `GET /films/{old ref}` is 404,
  `GET /sitemap.xml` does not contain the ref, `GET /films/search?q=<title>` does not return
  it. (Router tests over the same session, or one end-to-end test that runs `purge` then
  hits the three routes.)

### Re-admission is closed (`tests/integration/ingest/imports/`)

- **New:** a Letterboxd row whose search hit names a **released film already in the catalog**
  with `release_date = 2008-12-25` is reported `unmatched` kind `outside_window`, makes no
  `/movie/{id}` request, and writes no `import_candidate` (D-1505.6's "already in the catalog"
  sentence, which no test covers today). The TMDB-account runner gets the same case off a
  summary date.
- Existing coverage already shows discover skips `Released`, the sweep enumerates undated
  films only, and the linker's candidate index reads live `catalog.film` rows; no new tests
  there. Cite them in the PR rather than duplicating.

### Docs

- `catalog/models.py` (`Film.tmdb_missing_at` docstring) and `app/models.py` (`Follow`
  docstring): one-sentence amendment each — films are never deleted once announced;
  NEU-1508 removed, once, the films imports had admitted already outside the window, which
  were never announced.
- `docs/specs/NEU-1505-imports-decline-released-films.md`: D-1505.7 struck and annotated
  (done in the planning session).
- `docs/specs/bl-entity-follows-project-spec.md`: EF-21 line annotated (done in the planning
  session).
- `CONTEXT.md`: **Import** entry gains the purge sentence (done in the planning session).
- `AGENTS.md` only if it lists the one-off scripts.

---

## 3. Design notes

### 3.1 Release date at creation, in SQL

```sql
SELECT f.id, f.tmdb_id, f.title, f.created_at,
       COALESCE(first_change.old_value #>> '{}', f.release_date::text)::date AS release_date_at_creation
FROM catalog.film f
LEFT JOIN LATERAL (
  SELECT c.old_value FROM catalog.film_field_change c
  WHERE c.film_id = f.id AND c.field = 'release_date'
  ORDER BY c.changed_at, c.id LIMIT 1
) first_change ON true
WHERE f.created_at >= :imports_shipped AND f.created_at < :before
  AND COALESCE(first_change.old_value #>> '{}', f.release_date::text) IS NOT NULL
  AND COALESCE(first_change.old_value #>> '{}', f.release_date::text)::date
      < (f.created_at AT TIME ZONE 'UTC')::date - :max_age_days
```

`old_value` is JSONB: a JSON `null` for "was undated" (which `#>> '{}'` renders as SQL NULL,
so the film is kept) or a JSON string date. **Amended at implementation:** the `COALESCE` above
falls through to the *current* `release_date` when that JSON `null` renders as SQL NULL, which
purges exactly the back-dated sweep films D-1508.1 keeps. The script branches on whether a
change row exists (`CASE WHEN first_change.id IS NOT NULL THEN old_value #>> '{}' ELSE
release_date::text END`); the undated-film test fails under the `COALESCE`. Write it with SQLAlchemy Core in the script, as
the precedent does; the SQL above is the meaning, not the code.

### 3.2 What the cascade takes

| Table | ondelete | Handled by |
|---|---|---|
| `catalog.film_genre`, `film_production_company`, `film_production_country`, `film_spoken_language`, `film_release_date`, `film_alternative_title`, `film_credit`, `film_video`, `availability_first_seen`, `film_availability_current` | CASCADE | cascade |
| `catalog.film_field_change`, `film_credit_change`, `film_company_change`, `film_release_date_change` | CASCADE | cascade (read by the audit first for the date-at-creation) |
| `news.event` → `event_story`, `event_summary`, `app.notification` | CASCADE | explicit delete after the audit (D-1508.6) |
| `news.story.film_id` | SET NULL | explicit UPDATE to NULL + `rejected` (D-1508.3) |
| `news.resolution_cache`, `ingest.credit_hold`, `ingest.link_retrieval_probe`, `app.import_candidate` | CASCADE | cascade |
| `app.follow` (`entity_type='title'`, `entity_id = film.id::text`) | no FK | explicit delete (D-1508.2) |

People, companies and collections that end up with no remaining film are left alone: they are
upserted entities, harmless without a film, and the entity sitemap only lists ones with a film
in play or in the window.

### 3.3 Why the import cannot bring them back

`ingest/imports/runner.py` checks the hit's date (`date_in_window`) before `propose_film`, so
a released film — in the catalog or not — is reported `outside_window` without a request
(D-1505.6). Discover's `classify_skip` drops `Released`; the sweep's enumerate phase lists
undated films only; the refresh phase re-reads films already in `catalog.film`; the linker's
candidate index is built from live film rows under `active_film_clause`. If TMDB ever
re-dated one of these films to the future, discover or the sweep could admit it as a new row
with a new UUID; that is a new, legitimate observation and the old follows do not return.

### 3.4 Frontend

Nothing to change. `routes/film.tsx` throws a 404 Response when `getFilm` returns null, and
its error boundary renders "Film not found". `/me/follows` never sees the deleted follows.

---

## 4. Out of scope / deferred

- A `410 Gone` for purged slugs (D-1508.4).
- Telling affected users their follows went (D-1508.2).
- Deleting or re-linking the stories (D-1508.3).
- Removing people, companies or collections left without films.
- A standing purge rule or a `purged` lifecycle state (D-1508.5); the sweep films TMDB
  back-dated after admission stay, as the ticket intends.
- The three local films with `status='Released'` and no date: not import films, not
  candidates.

---

## 5. Deploy notes

- Merge, deploy, then in the api container: `python scripts/purge_imported_released_films.py`
  (dry), read the audit, paste it on the PR, then `--apply`. If the deploy lands after
  2026-09-28, pass `--before` with NEU-1505's actual deploy timestamp — the default only
  bounds the window the import could have written in.
- Verify: the four Fincher pages 404 at https://backlotter.com/film/4922-…, `/sitemap.xml` no
  longer lists them, search for "Benjamin Button" returns nothing.
- No migration, no Coolify changes, no frontend release.
