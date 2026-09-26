# NEU-1393 — Test suite spends ~10-20s deriving argon2 password hashes at production cost

**Project:** bl: Maintenance · **Milestone:** none · **Repo:** upcoming-movies-backend
**Related:** NEU-1346 (discovery context only)

## Problem

`src/upmovies/app/passwords.py` builds one module-level `_hasher = PasswordHasher()` with
argon2-cffi's production defaults (t=3, m=64 MiB, p=4). Measured in the dev container a hash
costs ~70 ms and a verify ~66 ms. `tests/fixtures/users.py::make_user` hashes on every user it
creates, and `authed_client` / `admin_authed_client` build on it; ~114 fixture call sites across
8 test files means roughly 10-20 s of the suite's ~83 s is spent deriving hashes whose strength
no test asserts. It never shows in `--durations` because it is spread thin.

Not the problem, don't re-investigate: schema setup is already session-scoped and per-test
teardown is one `TRUNCATE`.

Decided (2026-09-17): **the suite swaps the module hasher from `tests/conftest.py`; production
code does not change.** Argon2 `verify` reads m/t/p from the hash string itself, so a cheap
fixture hash also verifies cheaply and the verify side needs no change. Only
`tests/unit/app/test_passwords.py` runs against the real `PasswordHasher()`.

Rejected: a `Settings`-driven cost (adds a production knob that could weaken hashing if
misconfigured, for a purely test-side concern) and a public `use_hasher()` seam in
`passwords.py` (test-only API in production code). Poking the private `_hasher` global from
conftest is the accepted trade: it is one line, next to a comment saying why.

## What to build

### 1. Cheap hasher for the whole suite — `tests/conftest.py`

- Add a session-scoped **autouse** fixture that replaces `upmovies.app.passwords._hasher` with
  `PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)` for the duration of the session
  and restores the original on teardown. m=8 KiB with p=1 is argon2's minimum valid
  configuration; expect a hash to drop from ~70 ms to well under 1 ms.
- Comment it the way `RATE_LIMIT_ENABLED` is commented at the top of conftest: the suite is not
  measuring password strength, and the one file that is opts back in (below).
- Nothing in `src/` changes. `hash_password` / `verify_password` keep reading the module global,
  which is what makes the swap take effect for `account_service`, `reset_service`,
  `email_change_service` and the `make_user` fixture alike.

### 2. Production-parameter tests — `tests/unit/app/test_passwords.py`

- Add a module-scoped autouse fixture that sets `passwords._hasher = PasswordHasher()` (real
  defaults) for this file's tests and restores the cheap one afterwards. The existing five tests
  (argon2id prefix, round-trip true, wrong password false, invalid hash false, two hashes
  differ) keep running at production cost — ~350 ms total, acceptable.
- Add **one regression test** guarding "production parameters are unchanged": hash a password,
  parse it with `argon2.extract_parameters`, and assert its `time_cost`, `memory_cost`,
  `parallelism` and `type` equal those of a freshly constructed `PasswordHasher()`. Compare
  against the library's defaults rather than hardcoding 3/65536/4 so an argon2-cffi upgrade
  that raises the defaults does not break the test — the property under test is "we use the
  defaults", not the specific numbers.

### 3. Measure

- Run `task test` before and after and put both wall-clock totals in the PR description.
  Target per the ticket: near 60-65 s from ~83 s. Also spot-check that `task test --
  --durations=12` no longer lists anything argon2-bound.

## Acceptance criteria

- [ ] `tests/conftest.py` installs the cheap hasher for every test by default; no test outside
      `tests/unit/app/test_passwords.py` derives a production-cost hash.
- [ ] `tests/unit/app/test_passwords.py` runs its round-trip/format tests against
      `PasswordHasher()` defaults, and a new test asserts a fresh hash carries exactly the
      library-default m/t/p/type.
- [ ] `src/upmovies/app/passwords.py` is untouched (`git diff --stat` shows no change under
      `src/`).
- [ ] Suite wall-clock is measurably down from ~83 s; before/after numbers recorded in the PR.
- [ ] `task format`, then `task test && task lint && task typecheck` green.

## Out of scope

- Any change to production hashing parameters, `Settings`, or the `passwords.py` API.
- A `real_hasher` pytest marker or other opt-in machinery beyond the single file-level
  fixture; add one only if a second file ever needs production parameters.
- Other suite hot spots — the ticket established there are none (slowest test 1.94 s is the
  deliberate model-graph import; next is a deliberate 1.0 s rate-limiter sleep).
