# NEU-1453 — `GET /me/import/active`: the caller's open import, or 204

**Ticket:** [NEU-1453](https://linear.app/neuroticsasquatch/issue/NEU-1453/backend-get-meimportactive-the-callers-open-import-job-or-204)
**Project:** bl: Entity Follows · **Milestone:** M5 — Imports you review · **Story:** NEU-1427
**Blocked by:** nothing · **Blocks:** NEU-1452 (frontend restore of a waiting review list, spec `../../../frontend/docs/specs/NEU-1452-open-import-restored.md`)
**Related:** NEU-1449 (two-phase job, merged PR #366), NEU-1450 (review list, frontend PR #171)
**Decisions honoured:** EF-22 (the import is two-phase; a new import discards an unconfirmed one), the M5 shared contract that `ACTIVE_IMPORT_STATUSES` includes `awaiting_review`
**Target repo:** upcoming-movies-backend · **Base branch:** `release/v1.0.0`
**Glossary:** `CONTEXT.md` — *Import*, *Open import* (seeded by this planning session)

## What to build and why

Since NEU-1449 every import stops at `awaiting_review` and writes no follows until its list is
confirmed. The only read of a job is `GET /me/import/{id}`, and the only holder of the id is the
`/welcome` page that started it. A reload, a second device, or cleared site data loses the id,
and the job sits open — following nothing, and counting as the user's one active import — until
their next upload supersedes it (NEU-1450's review found this; NEU-1452 is the ticket).

The frontend needs to ask, without an id, "does this account have an import going?" The backend
already knows: `import_job_repo.active_for_user` reads the one row in `ACTIVE_IMPORT_STATUSES`
(`queued`, `running`, `awaiting_review`), and the partial unique index on that set guarantees
there is at most one. This ticket exposes it.

Option B of NEU-1452 was chosen over a per-browser localStorage restore because the backend half
is one route over an existing query, and it retires the "an import started on a phone is not
reported on a laptop" limitation for the case that matters — a list waiting to be confirmed.

## The route

`GET /me/import/active` on the existing `/me/import` router, so the router's `entitled`
dependency applies unchanged (401 anonymous, 403 unentitled, exactly like the by-id read).

| Caller has | Answer |
|---|---|
| a job in `queued`, `running` or `awaiting_review` | **200** `ImportJobOut`, built by `_job_out`, so `candidates` is filled while the job is `awaiting_review` and empty otherwise |
| no such job (never imported, or every job `succeeded` / `failed`) | **204**, no body |

Rules:

- **204, not 404.** Having no open import is the ordinary state of an account, not a lookup
  miss, and the frontend treats it as "nothing to restore" rather than as an error.
- **Declared before `GET /{job_id}`** in `routers/imports.py`. FastAPI matches routes in
  declaration order; declared after, the literal `active` is handed to the UUID parser and
  answers 422.
- **Not on the `import` rate-limit bucket**, for the reason the by-id poll's docstring gives:
  that bucket is six an hour and this read happens on every `/welcome` and timeline mount.
- No new repo function: `active_for_user` is the query. No new DTO: `ImportJobOut` is the
  shape, and the frontend's `ImportJob` type already reads it.
- The route has no side effects. It does not expire, discard or touch the job.

## Acceptance criteria

Integration tests in `tests/integration/routers/test_imports.py`, alongside the by-id read's:

1. An entitled user with no jobs gets 204 and an empty body.
2. With a job in `awaiting_review` and candidate rows, 200 with that job, `status` =
   `awaiting_review`, and `candidates` listing the rows (selected and skipped alike).
3. With a job in `running`, 200 with that job and empty `candidates`.
4. Once that job is `succeeded` (or `failed`, including `error = superseded`), 204.
5. Another user's open job is never returned: user B gets 204 while user A's job is open.
6. Anonymous and unentitled callers get the router's usual 401 / 403.
7. The literal path is reachable: a request to `/me/import/active` does not answer 422 (covers
   the declaration-order rule).

Before claiming done: `task format`, then `task test && task lint && task typecheck`, in the
container.

## Deploy

Backend before frontend, the ordinary rule. The frontend change (NEU-1452) is additive: the
frontend shipped today never calls this route, and the route changes nothing the frontend
already reads. No Coolify variable, no migration.

## Out of scope

- Expiring `awaiting_review` jobs on a clock (project spec §8, still open).
- A "latest job of any status" read. That would let the frontend delete its per-browser TMDB
  provenance storage (`lib/tmdb-import.ts`), but it changes what "last imported from" means and
  was deliberately not taken. The storage stays for the provenance line only.
- A list endpoint for import history.
