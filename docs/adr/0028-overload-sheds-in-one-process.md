# ADR-0028 — The API sheds overload in one process instead of adding workers

**Status:** Accepted 2026-11-10 (NEU-2271), amended 2026-11-13 (NEU-2285) and 2026-11-14
(NEU-2291) · **Spec:** `../specs/NEU-2271-db-pool-fail-fast.md`,
`../specs/NEU-2285-in-flight-cap.md`

## Context

A crawl burst (Lightpanda, 2026-11-09 and -10) sent a few hundred SSR page loads at the API in
three minutes. Prod runs one uvicorn process on an 8-core host. Person reads take ~50–150 ms
unloaded, yet requests waited the full 30 s for one of the pool's 15 connections and failed as
500s (`../measurements/2026-11-10-neu-2271-pool-baseline.md`). Each request's session held its
connection until the request ended, so a saturated process kept connections checked out long
after their queries finished.

Adding uvicorn workers is the obvious way to use the idle cores, and it was rejected.

## Decision

The API stays **one process** and handles overload by **holding connections only as long as
their queries** and **failing fast**:

- Public read routes end their transaction once the service returns, before the response is built
  and sent.
- The pool is explicit and sized against the dedicated Postgres's `max_connections`, with a short
  checkout timeout.
- A pool checkout timeout answers **503 with `Retry-After`**. Overload is a client-retryable
  condition, not a server fault.

## Why not more workers

- **The rate limiter is in-process.** `app/rate_limit.py` keeps per-IP token buckets in memory.
  With N workers each visitor gets N independent buckets, and which one a request hits depends on
  the worker that happens to accept it. Every limit becomes roughly N× and nondeterministic.
- **Lifespan work is per process.** `lifespan` cancels runs orphaned by a crash and opens the
  process-wide `MailGateway`. Under N workers the orphan sweep runs N times concurrently at boot.
- **Connections multiply.** Each worker has its own pool, so N workers need N× the connection
  budget against the same `max_connections` — with no overload handling added.

## Consequences

- Throughput is bounded by one core. If a measured burst shows the process itself (not the pool)
  saturating after this change, revisit with a shared rate-limit store first; that is what makes
  workers safe.
- Edge caching of public pages is the bigger lever against crawls. It is separate work: the
  server-rendered document varies on `timeline_hint` (ADR-0026).
- Clients see 503s during a burst. The frontend's loaders render them as a "busy" page
  (NEU-2273).
- The pool timeout alone does not bound how many requests the process admits; an in-flight cap
  now does (NEU-2285, amendment below).

## Amendment (2026-11-13, NEU-2285): an admission cap

The pool timeout bounded how long a request waits for a connection, not how many requests the
one process accepts. On 2026-11-13 a burst from a wide spread of IPs put the event loop so far
behind that `/healthz`, which does no DB work, missed the Docker health check for long enough
that Traefik took the container out of rotation. Every visitor then got Traefik's plain 502/503
instead of the busy page. The process, not the pool, was the bottleneck: late in the burst the
pool had idle connections while requests still timed out.

So the one process now also has an **admission cap**. `overload.InFlightCap` admits at most
`MAX_IN_FLIGHT_REQUESTS` (default 60, twice the pool) requests at once and answers the rest with
the same `503 overloaded` + `Retry-After`, before routing, dependencies or the database.
`/healthz` is exempt and never counted. A shed request costs the loop almost nothing, so the
loop stays short enough for the probe to be answered and the container stays in rotation,
serving the busy page instead of a proxy error.

> **Amended 2026-11-14 (NEU-2291): the cap's default is now 30, the pool's size.** NEU-2286
> measured what the 60 above had reasoned: the process saturates at about 10 in flight, so
> slots past that add only waiting. In an open-loop replay of the crawl at 1.1–1.4× its
> admissions in prod terms, 30 kept the same throughput as 60, halved admitted latency and
> missed no `/healthz` probe in three runs (60 missed six in one). With admission equal to the
> pool, no admitted request waits for a connection, so every overload answer is an immediate
> shed rather than the pool's 503 after 3 s. Evidence:
> `../measurements/2026-11-13-neu-2286-api-throughput.md` §3, §4, §7.

Rejected: uvicorn's `--limit-concurrency`, which counts connections (Traefik holds keep-alives
open) and answers a plain-text 503 the frontend can't tell from an outage; and loosening the
health check alone, which keeps a saturated process in rotation without making it answer.

The first consequence above still holds: if the process saturates *below* the cap, the next
step is a shared rate-limit store, then workers. Why throughput under load is low is NEU-2286.
