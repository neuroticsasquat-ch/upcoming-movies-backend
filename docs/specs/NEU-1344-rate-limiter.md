# NEU-1344 — Rate limiter dependency on auth, import, and public read routes

**Project:** bl: Consumer Pivot · **Milestone:** M1 — Accounts and the delivery pipe
**Project spec:** `docs/specs/bl-consumer-pivot-project-spec.md` (D-19) · **Story:** NEU-1328
**Sibling ticket (frontend):** the Worker-side signing described in §4 ships separately as
"Worker signs SSR API requests and forwards the visitor IP" (upcoming-movies-frontend).

## Problem

Nothing outside the per-email login lockout (`app.LoginAttempt`, `account_service.py`) is
metered. Open signup (NEU-1343) removes the invite gate, so signup, password reset,
verification re-sends, the imports, and the public read endpoints all become unmetered public
surface. One per-IP limiter has to cover them.

Two facts about the deployment shape the design, and both were checked on 2026-09-15:

1. **The API is not behind Cloudflare.** `api.backlotter.com` resolves straight to the Hetzner
   box; requests reach uvicorn through Coolify's Traefik only. Uvicorn runs a single process
   (`Dockerfile` prod CMD, no `--workers`) **without** `--proxy-headers`, so
   `request.client.host` is Traefik's address today — which also means `login_attempt.ip`
   currently records the proxy, not the user.
2. **The site is server-rendered on a Cloudflare Worker** (`frontend/workers/app.ts`). Its
   loaders fetch `/feed/grouped` and `/films/{ref}` from `env.API_BASE_URL` with only an
   `Accept` header, from Cloudflare egress IPs. A naive per-IP limiter would throttle the
   entire anonymous site through a handful of shared addresses.

## What to build

### 1. `rate_limit(bucket)` dependency

A FastAPI dependency factory in `upmovies/app/rate_limit.py`:

```python
@router.post("/signup", dependencies=[Depends(rate_limit("signup"))])
```

- **Algorithm:** token bucket per `(bucket, client key)`. Each bucket has `capacity` (burst)
  and `refill_per_minute`. A request takes one token; an empty bucket → **HTTP 429** with
  `Retry-After: <seconds until one token>` and a JSON body `{"detail": "rate_limited",
  "bucket": "<name>", "retry_after": <int>}`.
- **Storage:** in-process, behind a small `RateLimitStore` protocol (`take(key) ->
  Decision`). The only implementation shipped is `MemoryStore` (dict + monotonic clock, LRU
  bounded at 50k keys). The API is one process, so this is exact today; the protocol exists
  so a Postgres store can be added if the API ever runs replicas — do **not** build it now.
- **Client key:** the resolved visitor IP (§3), or `"anon"` if none can be resolved (should
  never happen behind Traefik; logged at WARNING once).
- **Config:** one settings entry per bucket in `config.py`,
  `RATE_LIMIT_<BUCKET>="<capacity>/<refill_per_minute>"` (string, parsed at startup;
  validation failure kills the container like every other config error). Plus
  `RATE_LIMIT_ENABLED` (default `true`) as the master switch and
  `RATE_LIMIT_PUBLIC_ENABLED` (default **`false`**, see §5).
- **Observability:** every 429 logs `rate_limited bucket=<b> key=<ip> retry_after=<s>` at
  INFO; a counter per bucket on the existing OpenTelemetry meter if one is wired, else skip.

### 2. Buckets and defaults

| Bucket | Applied to | Default (`capacity / refill per minute`) |
|---|---|---|
| `signup` | `POST /auth/signup` | `5 / 0.083` (5 per hour) |
| `login` | `POST /auth/login` | `20 / 1.33` (20 per 15 min) — on top of the per-email lockout, unchanged |
| `auth_request` | reset request, verify re-send, email-change request (NEU-1339/1340/1341 routes) | `5 / 0.083` |
| `import` | `POST /me/import/letterboxd`, `GET /me/import/tmdb/start` (M3; register the bucket now, apply when those routes land) | `6 / 0.1` |
| `public` | `GET /feed`, `/feed/grouped`, `/films/search`, `/films/{ref}`, `/calendar`, and the M3 `/people/*`, `/companies/search`, `/collections/search` | `240 / 120` |
| `ics` | `GET /calendar/{token}.ics` (M7; register now) | `30 / 30` |

Settings hold the raw string; a helper exposes `(capacity, refill_per_minute)`. Sitemap is
excluded (crawlers, cached upstream). `/me/*` authenticated CRUD is not limited by this ticket.

### 3. Resolving the client IP

- **Prod CMD** gains `--proxy-headers --forwarded-allow-ips='*'`. The API port is reachable only
  via Traefik on the Docker network, so trusting every proxy hop is safe; uvicorn then sets
  `request.client.host` from `X-Forwarded-For`. This fixes `login_attempt.ip` as a side effect
  — no code change needed there. Add the same flags to the dev CMD for parity.
- `client_ip(request) -> str` in `rate_limit.py` returns `request.client.host`, **unless** the
  request carries a valid Worker signature (§4), in which case it returns the forwarded visitor
  IP. All limiter keys go through this helper; nothing else reads headers.

### 4. Worker-signed requests

- Setting `SSR_ORIGIN_SECRET: str | None` (default `None`). When set, a request whose
  `X-Backlotter-Origin` header equals the secret (constant-time compare) is a **signed**
  request, and its `X-Backlotter-Client-IP` header is taken as the visitor IP for limiting.
- The visitor IP travels in a **dedicated header, not `X-Forwarded-For`**: Traefik strips
  forwarded headers from senders outside its trusted set, and the Worker's egress IPs are not
  in it, so `X-Forwarded-For` from the Worker would never survive the hop.
- A signed request with a missing or malformed client-IP header falls back to
  `request.client.host` (the egress IP) and logs once at WARNING.
- Signed requests are otherwise ordinary: they hit the same buckets with the visitor's key.
- The frontend sibling ticket adds both headers to the loader fetches only (browser-side
  fetches are already per-visitor). Wrangler secret name: `SSR_ORIGIN_SECRET`.

### 5. Safe rollout order

The backend can merge and deploy **before** the Worker signs requests, without throttling the
site, because `RATE_LIMIT_PUBLIC_ENABLED` defaults to `false`: the `public` bucket is wired but
inert. Order:

1. Deploy backend (this ticket): auth/import buckets live, public bucket inert, proxy headers on.
2. Set `SSR_ORIGIN_SECRET` in Coolify and as a Wrangler secret; deploy the frontend sibling.
3. Flip `RATE_LIMIT_PUBLIC_ENABLED=true` in Coolify and restart.

Add this to the deploy checklist in `AGENTS.md` (Coolify shadows compose fallbacks; the flag and
the secret must be set in the UI).

## Acceptance criteria

- A burst past a bucket's capacity from one IP returns 429 with a correct `Retry-After`; the
  next IP is unaffected; tokens refill at the configured rate (test with an injected clock).
- Signup, login, and the auth-request routes are limited by their buckets; login lockout
  behaviour is unchanged and tested alongside.
- With `RATE_LIMIT_PUBLIC_ENABLED=false` (default) no public route ever 429s.
- With it `true`: unsigned public requests key on `request.client.host`; a request with a valid
  `X-Backlotter-Origin` keys on `X-Backlotter-Client-IP`; an invalid secret is ignored (keyed on
  the socket address, never rejected).
- Prod and dev CMDs carry `--proxy-headers --forwarded-allow-ips='*'`; a test with a spoofed
  `X-Forwarded-For` through the ASGI test client shows `client_ip()` honouring it.
- `RATE_LIMIT_ENABLED=false` bypasses everything (for tests that hammer endpoints).
- Bad bucket strings fail startup validation.
- `task format`, then `task test && task lint && task typecheck` pass.

## Out of scope

- Postgres or Redis-backed buckets (protocol only).
- Limiting authenticated `/me/*` CRUD.
- Turnstile (NEU-1343) and the frontend's 429 handling (NEU-1345).
- Trusting Cloudflare `CF-Connecting-IP` — the API is not behind Cloudflare.
- Per-user (as opposed to per-IP) limits.
