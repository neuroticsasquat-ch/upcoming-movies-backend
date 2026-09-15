# NEU-1356 — Letterboxd import with /search/movie resolution and unmatched report

**Project:** bl: Consumer Pivot · **Milestone:** M3 · **Story:** NEU-1331
**Project spec:** `docs/specs/bl-consumer-pivot-project-spec.md` (D-15) · **Blocked by:** NEU-1349
**Consumers:** NEU-1357 (TMDB import reuses the job and derivation helpers), NEU-1358 (onboarding
UI polls the job).

## Problem

Cold start is the make-or-break of the pivot: a new user should go from signup to a populated
timeline with one upload. Letterboxd's export gives two files that matter: `watchlist.csv`
(`Date, Name, Year, Letterboxd URI`) and `ratings.csv` (same plus `Rating`, 0.5–5 in halves).
Neither carries a TMDB id, so every row must be resolved by title and year, and deriving person
follows from a rated film needs that film's credits — one details fetch per film, at the
client's 40 req / 10 s limiter. A thousand-row ratings file is minutes of work; it cannot run
inside the upload request.

Two decisions (2026-09-15):

1. **Import is a background job with a pollable status**, following the repo's existing
   `asyncio.create_task` + tracking-row pattern (`routers/ingest_admin.py`).
2. **Rated films contribute people only.** The catalog is the upcoming-film spine; a rated
   back-catalog film is fetched for its credits and discarded. Watchlist films are upserted in
   full, because they feed the provider poll set (D-27) and the user wants to be told about them.

## What to build

### 1. Import job

- Table `app.import_job (id UUID, user_id, source ∈ letterboxd|tmdb, status ∈
  queued|running|succeeded|failed, rows_total, rows_done, watchlist_created, follows_created,
  unmatched JSONB [{name, year, kind ∈ watchlist|rating}], error, created_at, started_at,
  finished_at)`. Registered in `upmovies/models.py`; migration.
- `POST /me/import/letterboxd` (authed + CSRF, `rate_limit("import")`, multipart, ≤ 5 MB):
  accepts either the export **zip** (pick `watchlist.csv` and `ratings.csv` out of it, ignore
  everything else) or one of those CSVs directly (identified by header row, not filename).
  Parses and validates the rows synchronously (bad file → 422 with a reason), inserts the job
  as `queued` with `rows_total`, schedules `run_letterboxd_import(job_id, settings)` with
  `asyncio.create_task`, returns **202** `{job_id}`. One running job per user; a second upload
  while one runs → 409.
- `GET /me/import/{job_id}` → the job row (own jobs only). The UI polls every 2 s while
  `queued|running`.
- The runner follows the pipeline contract: its own `session_factory`, commit per row, a
  time-throttled heartbeat on `rows_done`, and it always finalizes (`failed` with `error` on
  crash — the task wrapper catches). It uses the shared `TMDBClient` from settings.

### 2. Title resolution

- `TMDBClient.search_movie(query, year) -> list[MovieSearchHit]` (`/search/movie`, params
  `query`, `primary_release_year`; first page only).
- Matching, in order, over the hits: (1) `title` or `original_title` equals the CSV name
  after **squash-fold** (`link/retrieval` normalization) **and** the hit's release year equals
  `Year`; (2) the same with year ± 1 (Letterboxd uses festival years); (3) otherwise
  **unmatched** — recorded, never guessed. Popularity breaks ties among exact matches.
- A `resolution` helper returns `(tmdb_id, title) | None`; it is pure over the hit list so it
  unit-tests without the network.

### 3. What each row produces

| Row | Resolution hit | Effect |
|---|---|---|
| watchlist.csv | matched | `movie_details` → `upsert_film` (full: film, credits, release dates, joins); `follow(title, film_id, source=letterboxd_import)`; `watchlist_item(film_id, source=manual, alert_prefs={stream})` |
| ratings.csv, `Rating ≥ 4.0` | matched | `movie_details` → upsert **only** the director(s) and the top-2 billed cast into `catalog.person` (reuse the person part of `_upsert_credits`); `follow(person, id, source=letterboxd_import)` for each. The film row is **not** written. |
| ratings.csv, `Rating < 4.0` | — | skipped without a fetch |
| any | unmatched | appended to `unmatched` |

- Films already in the catalog skip the details fetch for the watchlist path only if
  `credits_observed_at` is recent (≤ 7 days); otherwise refresh via `upsert_film` as usual.
- Ratings-path people: if all needed people already exist in `catalog.person` **and** the film
  is in the catalog with credits, read credits locally and skip the fetch.
- Director = `crew` with `job == "Director"`; top-2 cast = `cast` with `order` 0 and 1.
- All inserts are idempotent (`follow` unique key; `watchlist_item` unique on `(user, film)`;
  `watchlist_dismissal` respected — a dismissed film is not re-added by import).
- Person follows from import do **not** trigger derived-watchlist derivation inline
  (NEU-1352 runs on the sweep pass; the import already added the films the user asked for).

### 4. Limits and cost

- Bucket `import`: 6 per hour per IP (NEU-1344).
- Rows: cap at 5,000 per file (422 above). At 4 req/s the worst case is ~20 minutes; the job
  status makes that visible instead of a hung request.
- The TMDB limiter is process-wide, so a running import slows the daily chain if they
  overlap; acceptable for v1 (documented in AGENTS.md), revisit if imports become frequent.

## Acceptance criteria

- A fixture export zip (watchlist of 3 with one unresolvable title; ratings of 6 with three
  ≥ 4.0) run against respx: job ends `succeeded`, `watchlist_created=2`, `follows_created`
  = 2 title follows + the distinct people from the three rated films, `unmatched` lists the
  one bad row with `kind=watchlist`; no `catalog.film` row exists for the rated-only films;
  their director and top-2 cast exist in `catalog.person`.
- Uploading the same zip again ends `succeeded` with zero new rows created.
- A dismissed film in the watchlist CSV is not re-added.
- A CSV with the wrong header → 422; a second upload during a running job → 409; polling a
  job you do not own → 404.
- A crash mid-run leaves the job `failed` with `error` set and earlier rows committed.
- `task format`, then `task test && task lint && task typecheck` pass.

## Out of scope

- Letterboxd URI → TMDB id resolution (would need scraping Letterboxd).
- Diary, reviews, lists, likes (only watchlist and ratings are read).
- Importing rated films into the catalog (decided against; see Problem).
- The onboarding UI (NEU-1358) — it polls `GET /me/import/{id}` and renders the counts and
  unmatched list from the job row.
