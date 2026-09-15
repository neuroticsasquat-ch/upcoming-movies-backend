# NEU-1357 — TMDB account import: v3 approve flow, watchlist and favorites

**Project:** bl: Consumer Pivot · **Milestone:** M3 · **Story:** NEU-1331
**Project spec:** `docs/specs/bl-consumer-pivot-project-spec.md` (D-16)
**Blocked by:** NEU-1349, NEU-1356 (reuses the import job and derivation helpers)

## Problem

Reading a user's TMDB watchlist and favorites needs TMDB's v3 user authorization: create a
request token → the user approves it on themoviedb.org → exchange it for a session id. That
session id can read **and write** the user's TMDB account. The original ticket said "stored
encrypted per user", but the backend has no encryption dependency and no secret-key convention;
adding both for one credential is real surface (`cryptography`, a key setting, rotation).

Decided (2026-09-15): **one-shot import, nothing stored.** The callback runs the import job and
the job's final step deletes the TMDB session. Re-import means re-approving on TMDB, which is a
one-click page for the user and removes a long-lived third-party credential from the database.

## What to build

### 1. Client methods (`ingest/tmdb/client.py`)

- `create_request_token() -> str` — `GET /authentication/token/new`.
- `create_session(request_token) -> str` — `POST /authentication/session/new`.
- `account(session_id) -> {id, username}` — `GET /account`.
- `account_watchlist_movies(account_id, session_id)` and
  `account_favorite_movies(account_id, session_id)` — paged
  (`/account/{id}/watchlist/movies`, `/account/{id}/favorite/movies`, `page` until
  `total_pages`), yielding `{id, title, release_date}`.
- `delete_session(session_id)` — `DELETE /authentication/session`.
All through the shared limiter and retry policy; respx tests for each.

### 2. Approve flow

- `GET /me/import/tmdb/start` (authed): calls `create_request_token`, stores it in
  `app.tmdb_auth_request (request_token PK, user_id, created_at)` (bound to the user so a
  callback cannot be replayed for someone else), and **302**s to
  `https://www.themoviedb.org/authenticate/{token}?redirect_to={TMDB_REDIRECT_URL}`.
  `TMDB_REDIRECT_URL` is a setting (prod: `https://backlotter.com/welcome?tmdb=callback`,
  the frontend page that then calls the callback route). Rows older than 15 minutes are
  ignored and pruned on the next start.
- `POST /me/import/tmdb/callback` (authed + CSRF, body `{request_token, approved}`): the
  frontend forwards TMDB's `request_token` + `approved=true` query params. Verifies the
  token belongs to the current user, exchanges it for a session id, reads `account`, inserts an
  `app.import_job(source=tmdb)` row (NEU-1356 table), and schedules `run_tmdb_import(job_id,
  session_id, account_id, settings)`. Returns **202** `{job_id}`. The session id lives only in
  the task's memory. `approved=false` or a denied token → 400, no job.
- One running job per user (409), same as Letterboxd.

### 3. The job

- Watchlist movies → the NEU-1356 watchlist path exactly (`upsert_film`, title follow,
  watchlist item), `source=tmdb_import`.
- Favorite movies → the NEU-1356 ratings path exactly (people only: director + top-2 cast →
  person follows, film discarded), `source=tmdb_import`. A favorite that is also on the
  watchlist gets both treatments.
- No title resolution is needed: TMDB ids are authoritative; `unmatched` stays empty. Films
  TMDB answers 404 for are skipped and counted in `unmatched` with `kind=tmdb_missing`.
- Row caps and idempotency as in NEU-1356. Job row records `tmdb_username` for the UI
  ("Imported from @user"); nothing else about the account is kept.
- **Final step, in a `finally`:** `delete_session(session_id)`; a failure to delete is logged
  at WARNING and does not fail the job. The job then finalizes `succeeded|failed`.

### 4. Removed from the original ticket

- No `app.tmdb_link` table, no encryption, no `DELETE /me/import/tmdb` unlink route. The
  frontend ticket NEU-1359 loses its "Unlink TMDB" affordance and instead shows the last
  TMDB import's date and username from the latest `import_job(source=tmdb)`.

## Acceptance criteria

- `start` stores a token bound to the user and redirects to the TMDB authenticate URL with
  the configured `redirect_to`.
- `callback` with another user's token → 403; expired token → 400; approved token → 202 and a
  queued job; the request-token row is deleted on use.
- With respx: 2 watchlist movies + 2 favorites (one overlapping) → 2 films upserted, 2 title
  follows, 2 watchlist items, person follows from both favorites' credits, no film row for the
  favorite-only movie, `delete_session` called exactly once even when a page fetch raises.
- Re-running with the same account creates nothing new.
- `task format`, then `task test && task lint && task typecheck` pass.

## Out of scope

- Persisting the TMDB session for re-sync (rejected; see Problem).
- TMDB lists, ratings (`/account/{id}/rated/movies`), or v4 access tokens.
- Writing anything back to the user's TMDB account.
