# NEU-1399 — Share one TMDB rate limiter across TMDBClient instances

**Project:** bl: Consumer Pivot · **Milestone:** none · **Repo:** upcoming-movies-backend
**Related:** NEU-1356 (Letterboxd import, merged — its §4 is corrected here), NEU-1357 (TMDB
account import, Todo — first consumer expected to use the new front door)
**Planned:** 2026-09-17

## Problem

`RateLimiter` is constructed inside `TMDBClient.__init__` (`ingest/tmdb/client.py:77`), so the
sliding window is per instance. Two clients alive at once each get a full
`TMDB_RATE_LIMIT_REQUESTS` / `TMDB_RATE_LIMIT_WINDOW_SECONDS` budget and together ask TMDB for
roughly double the intended rate; N clients ask for N times it.

**The ticket's framing of who collides is wrong, and the correction changes what this ticket can
deliver.** The ticket, the `AGENTS.md` gotcha and the `runner.py` docstring all describe the
collision as "a Letterboxd import overlapping the daily chain". Those two never share a process:
the daily, hourly and sweep chains are separate `python -m upmovies.pipeline_run` invocations run
as Coolify scheduled tasks (`pipeline_run.py:452`, ADR-0003). **No process-level fix can make
those two share a budget**, and nothing in this ticket tries to.

What a process-wide limiter actually bounds is the **API process**, where the concurrency is
real and currently unbounded:

- `routers/imports.py:113` — `asyncio.create_task(run_letterboxd_import(...))`, one task per
  upload. Two users uploading at once is 2× the configured rate; ten is 10×. This is the
  sharpest case today and the ticket does not name it.
- `routers/ingest_admin.py:39/51/62/73` — `/admin/ingest/*` spawns the *same* stage runners as
  background tasks **in the API process**. An admin-triggered tmdb or sweep stage racing an
  import is a genuine in-process double.
- NEU-1357's TMDB account import lands in this process too.
- D-27's provider poll does **not**: the project spec puts it "daily, in the sweep's slot", which
  is the sweep process, whose phases run sequentially. It is not the third concurrent consumer
  the ticket claims — it is a fourth separate process.

**Impact stays low**, which is why this is not urgent: `TMDBClient._request` honours `Retry-After`
on 429 without spending its retry budget (`client.py:101`), so the failure mode is a slower pass,
not a broken one. The cost is politeness and headroom.

## Decisions (settled in grilling, 2026-09-17)

- **D-1 Scope: bound the process, tell the truth about the rest.** Ship the shared limiter so
  every consumer inside one process shares one window. Do not build a cross-process limiter; the
  deployment can still exceed the configured rate by the number of live processes, and the docs
  must say so rather than imply the problem is gone.
- **D-2 Mechanism: `TMDBClient.from_settings(settings)` is the front door.** A classmethod
  (repo precedent: `AdmissionTranches.from_settings`) that builds the client and hands it the
  shared limiter, plus an optional `limiter=` parameter on `__init__`. Sharing is defined as
  *"clients built from the app settings share"* — the actual intent — rather than *"clients whose
  numbers happen to match"*. Direct `TMDBClient(...)` construction keeps its own window, so no
  existing test changes behaviour.
- **D-3 Memoise on `(rate_calls, rate_window)`, not a bare singleton.** Production has one
  `Settings` (`get_settings` is `lru_cache`d) and therefore exactly one limiter. Keying means the
  sharing test's 2-per-0.3s window and the suite's configured window are separate entries, so
  neither can leak into the other — in particular a test that overrides the limits cannot leave a
  tiny window behind for whatever runs next, which a bare singleton would unless every such test
  reset on teardown as well as on entry.
- **D-4 Tests get a large limit in `conftest.py`.** The suite is one process, so the shared window
  becomes cumulative across the six integration modules that build clients from settings. Neutralise
  it the way the inbound limiter already is (`tests/conftest.py:11`, `RATE_LIMIT_ENABLED=false`).
- **D-5 Prove it behaviourally, on the real clock.** Assert the wait, not the wiring — mirroring
  `test_rate_limiter_enforces_rate`, which already spends real time.
- **D-6 Pin the production call sites with a test.** Sharing only happens through `from_settings`,
  so a future consumer that constructs a client the old way silently reintroduces the bug with no
  test failing. One structural test closes that.

Rejected: a Postgres-backed cross-process window (a new shared dependency on the hot path of every
TMDB request for a politeness problem; NEU-1344 deferred the equivalent for the inbound limiter
with "do not build it now"); a registry keyed on `(base_url, calls, window)` reachable from the
plain constructor (sharing implicit in a global, and test isolation resting on no two suites
picking the same numbers — true today only by luck: 20, 50, 100 and 1000 per second);
docs-only (leaves N concurrent imports at N× the rate); an injected clock in `RateLimiter`
(reshapes a class every ingest path depends on, for one test); renaming the raw constructor
(spends the ticket on touching five test call sites).

## What to build

### 1. Shared limiter and front door — `ingest/tmdb/client.py`

```python
_SHARED_LIMITERS: dict[tuple[int, float], RateLimiter] = {}


def _shared_limiter(calls: int, window: float) -> RateLimiter:
    key = (calls, window)
    if key not in _SHARED_LIMITERS:
        _SHARED_LIMITERS[key] = RateLimiter(calls, window)
    return _SHARED_LIMITERS[key]


def reset_shared_limiters() -> None:
    """Drop every shared window. Tests only."""
    _SHARED_LIMITERS.clear()
```

`TMDBClient.__init__` gains `limiter: RateLimiter | None = None` (keyword-only, last), and uses
`limiter or RateLimiter(rate_calls, rate_window)` — so the existing constructor signature and its
per-instance behaviour are unchanged for every current caller.

```python
    @classmethod
    def from_settings(cls, settings: Settings) -> "TMDBClient":
        """Build a client sharing this process's TMDB window."""
```

It passes `base_url`, `api_key`, `rate_calls`, `rate_window` and `retry_max_attempts` from
settings — the same five values the call sites pass today — and
`limiter=_shared_limiter(settings.tmdb_rate_limit_requests, settings.tmdb_rate_limit_window_seconds)`.

The class docstring states the rule: production builds clients with `from_settings`; the raw
constructor is for tests and for a deliberately independent budget. Note that `client.py` must not
import `Settings` in a way that creates a cycle — `config.py` imports nothing from `ingest`, so a
plain `from upmovies.config import Settings` is fine.

### 2. Call sites

Replace the five-line construction block with `TMDBClient.from_settings(settings)` at:

- `pipeline_run.py:85` (`run_tmdb_stage`)
- `pipeline_run.py:192` (`run_sweep_stage`)
- `ingest/imports/runner.py:146` (`run_letterboxd_import`)
- `scripts/probe_undated_candidates.py:206` — a fourth site the ticket does not list. It runs as
  its own process, so sharing buys it nothing today, but it builds from settings identically and
  leaving it out would make the pinning test in §4 lie.

### 3. Test environment — `tests/conftest.py`

Beside the existing `RATE_LIMIT_ENABLED=false` line, and with a comment saying why: set
`TMDB_RATE_LIMIT_REQUESTS` high (e.g. `"100000"`) before settings are read, so the now-cumulative
shared window is inert for the tests that are not about it. Leave
`TMDB_RATE_LIMIT_WINDOW_SECONDS` alone.

### 4. Tests — `tests/unit/ingest/tmdb/test_client.py`

- **Sharing waits.** `reset_shared_limiters()`, build a `Settings` (or monkeypatched copy) at
  2 requests / 0.3 s, `TMDBClient.from_settings(...)` twice, respx-mock one endpoint, issue three
  requests across the two clients, assert elapsed ≥ 0.3 s.
- **Independence holds.** Two clients built by direct construction at the same 2/0.3 s do *not*
  wait for three requests (assert elapsed well under 0.3 s).
- **Pinned call sites.** Scan `src/upmovies/**/*.py` and `scripts/**/*.py` for `TMDBClient(`;
  assert the only hit is inside `ingest/tmdb/client.py` itself. The assertion message says what to
  do instead: *build TMDB clients with `TMDBClient.from_settings(settings)` so they share the
  process-wide rate limiter.*

### 5. Docs

- **`AGENTS.md:105–117`** — rewrite the gotcha rather than delete it. It now reads: the TMDB
  limiter is per **process**. Clients built by `TMDBClient.from_settings` share one window, so the
  API process's concurrent consumers (Letterboxd/TMDB imports, `/admin/ingest/*` triggers) share a
  single budget. Each `pipeline_run` invocation is a separate process with its own window, so a
  deployment running the API plus a scheduled chain can still ask TMDB for more than the configured
  rate — bounded by the number of live processes, not by the number of clients. Cross-process
  limiting is deliberately not built.
- **`ingest/imports/runner.py`** — delete the second paragraph ("That budget is this client's own…").
  AGENTS.md carries the fact; the original entry's own "recorded in AGENTS.md rather than repeated
  here" applies.
- **`docs/specs/NEU-1356-letterboxd-import.md` §4, line 82** — amend in place with a dated
  correction (the repo's convention: ADR-0003, NEU-1354, NEU-1356's own update notes). The line
  claimed the limiter was already process-wide and that an import merely slows the daily chain;
  both halves were wrong. Replace with: the limiter was per client until NEU-1399 made it
  per process, and an import never shared the daily chain's budget because the chain is a separate
  process.

## Acceptance criteria

- Two clients from `TMDBClient.from_settings` with capacity N share one window: the N+1th request
  across the pair waits. Two directly-constructed clients at the same limits do not.
- The four production/script call sites build via `from_settings`; the pinning test fails if a new
  one does not.
- `AGENTS.md`'s gotcha states the per-process truth including the cross-process caveat; the
  `runner.py` paragraph is gone; NEU-1356 §4 carries the dated correction.
- Suite runtime is not materially changed by the shared window (D-4).
- `task format`, then `task test && task lint && task typecheck` pass.

## Out of scope

- **Cross-process rate limiting.** A Postgres- or Redis-backed window shared by the API process and
  the scheduled invocations. Named in AGENTS.md as a known gap, not built.
- **Weighting or priority between consumers.** The shared limiter is FIFO (`asyncio.Lock`), so an
  import and an admin-triggered stage interleave fairly; an import cannot monopolise the window,
  because each consumer issues its requests serially, so queue depth is the number of concurrent
  consumers rather than the number of rows. Giving imports a smaller share is a separate decision
  if it ever matters.
- **Changing `RateLimiter`'s algorithm, clock, or its lock-held-across-sleep shape.**
- **`TMDB_RATE_LIMIT_*` retuning.** Whether 40/10 s is the right budget is untouched.
- **NEU-1357 and D-27 wiring.** They inherit the front door when they land.

## Not needed, decided during design

- **No ADR.** Reversible, small, and unsurprising to a future reader given the docstring and the
  AGENTS.md entry — it fails all three of the ADR test's conditions.
- **No `CONTEXT.md` entry.** Rate limiting is infrastructure, not domain vocabulary, and the
  glossary is kept free of implementation detail. The inbound/outbound ambiguity (`app/rate_limit.py`'s
  per-IP token bucket vs this outbound sliding window) is handled by the AGENTS.md rewrite naming
  the TMDB one explicitly.
