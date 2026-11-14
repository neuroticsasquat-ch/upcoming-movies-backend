# NEU-2285 — Shed requests past an in-flight cap before they touch the DB

**Ticket:** NEU-2285 · **Builds on:** NEU-2271 (`NEU-2271-db-pool-fail-fast.md`) · **ADR:**
`../adr/0028-overload-sheds-in-one-process.md` (amended) · **Related:** NEU-2284 (frontend
renders proxy 502/503s as the busy page), NEU-2286 (why throughput under load is low)

## Problem

On 2026-11-13 at ~10:44 UTC a crawl burst from a residential-proxy-style spread of IPs
saturated the API's one uvicorn process. NEU-2271's pool-timeout 503 fired (82 of 679 access
lines), but only after each request had waited `DB_POOL_TIMEOUT` (3 s). Nothing bounded how
many requests the process accepted, so the event loop fell behind far enough that
`GET /healthz` — no DB work — went unanswered for ~590 access-log lines, then six queued probes
were served back-to-back. Docker marked the container unhealthy, Traefik took it out of
rotation, and the frontend logged ~740 plain 502/503s from Traefik. The ticket has the full
evidence.

## Decisions

**D-2285.1 — An in-flight cap at the front of the app.** `overload.InFlightCap`, a pure ASGI
middleware (not `BaseHTTPMiddleware`, which runs the app in a second task and buffers through
a stream), counts requests in flight. Past `MAX_IN_FLIGHT_REQUESTS` it answers at once with
the existing overload response — `503`, `{"detail": "overloaded", "retry_after": 5}`,
`Retry-After: 5` — before routing, dependencies or the database. The frontend already renders
that body as its busy page (NEU-2273).

- `/healthz` is exempt: never counted, never shed.
- The count needs no lock: check and increment run with no `await` between them on one loop.
  A request leaves the count when the app returns, after its response is sent.
- Registered **inside** `CORSMiddleware` (added first in `create_app`), so CORS still answers
  preflights itself and a shed 503 carries `Access-Control-Allow-Origin`: a browser reads it as
  overload, not a network error. GZip stays outermost.

**D-2285.2 — Default 60, a setting.** `max_in_flight_requests` / `MAX_IN_FLIGHT_REQUESTS`,
`ge=1`, default **60**: twice the pool's 30 connections (10 + 20 overflow). Public reads hand
their connection back before the response is built (D-2271.2), so up to a pool's worth more
requests can be serializing or sending while the pool stays busy. Beyond that a request would
only queue for a connection and 503 after 3 s, having spent loop time `/healthz` needs.
Prod runs the default; like `DB_POOL_*` it is not seeded in `docker-compose.prod.yml`. NEU-2286
can tune it from Coolify by adding the variable.

> **Amended 2026-11-14 (NEU-2291): the default is now 30**, the pool's size, measured rather
> than reasoned. NEU-2286 found the process saturates at about 10 in flight, so the extra
> slots this decision argued for add waiting and no throughput. In an open-loop replay of the
> 2026-11-13 crawl (1.1–1.4× its admissions in prod terms), 30 gave the same throughput as 60,
> halved admitted latency (p50 0.27 s against 0.64 s) and missed no `/healthz` probe in three
> runs, where 60 missed six in one. At 30 no admitted request waits for a connection, so the
> pool's 3 s 503 can't happen and every overload answer is an immediate shed. The code default
> changed rather than a Coolify variable, so code, spec and prod agree (D-2286.9). Evidence:
> `../measurements/2026-11-13-neu-2286-api-throughput.md` §3, §4, §7.

**D-2285.3 — Visible in Bugsink under its own fingerprint.** Same pattern as D-2271.5: a
warning log per shed request (path, in-flight count, limit), and at most one
`capture_message("In-flight request cap reached", level="warning")` per 60 s per process, with
fingerprint `["in-flight-cap"]`. The throttle is now keyed per fingerprint
(`overload._last_capture`), so a burst that trips both the cap and the pool sends one event for
each.

**D-2285.4 — The health check is recorded, not loosened.** The cap is the fix; a laxer probe
only keeps a saturated process in rotation. Loosen only if a load test with the cap in place
still shows a missed probe.

## Prod health check settings

As declared in `docker-compose.prod.yml`, which Coolify deploys from. Not yet confirmed
against the dashboard or the running container — see below:

| Setting | Value |
|---|---|
| Probe | `python -c "urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"`, exit 0 on 200 |
| `interval` | 10 s |
| `timeout` | 3 s (the probe's own `urlopen` gives up at 2 s) |
| `retries` | 3 |
| `start_period` | 30 s |

So three consecutive probes that don't get a 200 within 2 s — about 30 s of a stalled loop —
mark the container unhealthy. That fits the incident: the pool warnings start at 10:43:56,
`/healthz` stops answering soon after, and the frontend's Traefik errors start at 10:44:20.

**To confirm by hand** (the agent's read of the running container was denied): on the prod
host, `docker inspect --format '{{json .Config.Healthcheck}}' <upmovies-backend container>`
should show the values above (durations in nanoseconds). If it differs, Coolify has overridden
the file; correct this table.

## Acceptance criteria

- [x] `MAX_IN_FLIGHT_REQUESTS` is a setting with a documented default, read by the middleware.
- [x] With the cap at 1 and one request held open in its session dependency, a second public
  request returns 503 `overloaded` with `Retry-After` and CORS headers, without reaching the
  session dependency (pool status unchanged); the slot frees when the held request ends.
- [x] With the cap reached, `GET /healthz` returns 200.
- [x] A shed request logs a warning; Bugsink capture is throttled and uses `in-flight-cap`,
  independent of the pool's throttle.
- [x] ADR-0028 is amended.
- [ ] Prod health check settings confirmed against the running container.
- [ ] After deploy, at the next crawl burst: `/healthz` answers throughout in `docker logs`, and
  the frontend Bugsink project gets no plain 502/503 issue for it.

## Out of scope

Cloudflare bot and rate-limit rules; why throughput under load is low (NEU-2286); frontend
handling of proxy 502/503s (NEU-2284); more uvicorn workers (ADR-0028).
